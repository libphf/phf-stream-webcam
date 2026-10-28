import os
import sys
import json
import time
import struct
import asyncio
import ipaddress
import threading
from collections import deque
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Any, Optional
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.server import serve as ws_serve

from camera_source import CameraSource
from audio_source import AudioSource
from encoder import PHFEncoder

# Packet types
PKT_VIDEO = 0x01
PKT_AUDIO = 0x02
PKT_JSON  = 0x03

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}

MACOS_CAMERA_HINT = ("No camera could be opened. On macOS, grant Camera access to the app "
                     "running Python (System Settings > Privacy & Security > Camera), "
                     "then restart it.")


def _is_local_name(hostname: str) -> bool:
    # mDNS names (*.local) resolve only on the local link, so a remote site cannot rebind them
    if hostname in LOOPBACK_NAMES or hostname.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def origin_allowed(origin: Optional[str], host_header: Optional[str], http_port: int) -> bool:
    """
    Accepts a WebSocket handshake only from the dashboard this server serves.
    Browsers always send Origin, so a missing one means a non-browser client
    (which no web page can drive). The origin host must equal the Host the
    client dialed and be an IP literal, localhost or an mDNS *.local name: any other
    domain name there means another site, possibly via DNS rebinding.
    """
    if origin is None:
        return True
    o = urlsplit(origin)
    if o.scheme not in ("http", "https") or not o.hostname:
        return False
    origin_port = o.port or (443 if o.scheme == "https" else 80)
    dialed_host = urlsplit("//" + (host_header or "")).hostname
    return (origin_port == http_port
            and o.hostname == dialed_host
            and _is_local_name(o.hostname))


class NoCacheHTTPHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, ws_port: int, **kwargs):
        self.ws_port = ws_port
        super().__init__(*args, **kwargs)

    def do_GET(self):
        # Runtime config so the client follows --ws-port
        if self.path.split("?", 1)[0] == "/config.json":
            body = json.dumps({"ws_port": self.ws_port}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format, *args):
        # Silence static file logs for cleaner console
        pass


class ClientSession:
    """
    Per-client outgoing queue drained by its own task. Control messages are
    always delivered, audio keeps a short backlog, and video keeps only the
    newest frame, so a slow client drops frames instead of stalling the others.
    """
    AUDIO_BACKLOG = 16  # ~370 ms of 1024-sample chunks at 44.1 kHz

    def __init__(self, websocket):
        self.ws = websocket
        self.wants_audio = False
        self._control = deque()
        self._audio = deque(maxlen=self.AUDIO_BACKLOG)
        self._video: Optional[bytes] = None
        self._wake = asyncio.Event()

    def push_control(self, data: bytes):
        self._control.append(data)
        self._wake.set()

    def push_audio(self, data: bytes):
        self._audio.append(data)
        self._wake.set()

    def push_video(self, data: bytes):
        self._video = data
        self._wake.set()

    async def run(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._control or self._audio or self._video is not None:
                if self._control:
                    data = self._control.popleft()
                elif self._audio:
                    data = self._audio.popleft()
                else:
                    data, self._video = self._video, None
                # Blocks only this client's task while its socket buffer drains
                await self.ws.send(data)


class StreamServer:
    def __init__(self, web_dir: str, host: str = "127.0.0.1", http_port: int = 8080, ws_port: int = 8765):
        self.web_dir = web_dir
        self.host = host
        self.http_port = http_port
        self.ws_port = ws_port

        self.camera_source = CameraSource()
        self.audio_source = AudioSource()
        self.encoder = PHFEncoder()

        self.clients: Dict[Any, ClientSession] = {}
        self._running = False
        self._httpd: Optional[ThreadingHTTPServer] = None
        # Serializes camera/microphone reconfiguration, which runs in worker threads
        # (created in run() so it belongs to the running event loop)
        self._device_lock: Optional[asyncio.Lock] = None

        # Performance trackers
        self._fps_counter = 0
        self._fps_last_time = time.perf_counter()
        self._measured_fps = 0.0

    def start_http(self):
        handler = lambda *args, **kwargs: NoCacheHTTPHandler(
            *args, directory=self.web_dir, ws_port=self.ws_port, **kwargs)
        self._httpd = ThreadingHTTPServer((self.host, self.http_port), handler)
        http_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        http_thread.start()
        print(f"[HTTP] Web dashboard serving at: http://localhost:{self.http_port}")

    def _check_origin(self, connection, request):
        origin = request.headers.get("Origin")
        if not origin_allowed(origin, request.headers.get("Host"), self.http_port):
            print(f"[WS] Rejected handshake from origin {origin!r}")
            return connection.respond(HTTPStatus.FORBIDDEN, "Origin not allowed\n")
        return None

    async def handle_client(self, websocket):
        session = ClientSession(websocket)
        self.clients[websocket] = session
        sender = asyncio.create_task(session.run())
        peer = websocket.remote_address
        print(f"[WS] Client connected from {peer}. Active clients: {len(self.clients)}")

        try:
            # Send initial device list and state to newly connected client
            init_msg = await asyncio.to_thread(self._build_device_list_msg)
            session.push_control(self._pack_json(init_msg))

            async for raw_msg in websocket:
                try:
                    data = json.loads(raw_msg)
                    action = data.get("action")

                    if action == "get_devices":
                        force = bool(data.get("refresh", False))
                        async with self._device_lock:
                            msg = await asyncio.to_thread(self._build_device_list_msg, force)
                        session.push_control(self._pack_json(msg))

                    elif action == "set_camera":
                        cam_id = data.get("id", 0)
                        w = int(data.get("width", 320))
                        h = int(data.get("height", 240))
                        fps = int(data.get("fps", 30))
                        print(f"[Control] Setting camera {cam_id} ({w}x{h} @ {fps}fps)")
                        async with self._device_lock:
                            ok, warning = await asyncio.to_thread(self.camera_source.start, cam_id, w, h, fps)
                        status_msg = {
                            "type": "camera_status",
                            "success": ok,
                            "warning": warning,
                            "info": self.camera_source.get_info()
                        }
                        self.broadcast(self._pack_json(status_msg))

                    elif action == "toggle_audio":
                        session.wants_audio = bool(data.get("enabled", True))
                        mic_id = data.get("device_id")
                        print(f"[Control] Audio enabled for {peer}: {session.wants_audio} (Mic: {mic_id})")
                        await self._update_audio(mic_id if session.wants_audio else None)
                        audio_status = {
                            "type": "audio_status",
                            "enabled": session.wants_audio and self.audio_source.is_enabled()
                        }
                        session.push_control(self._pack_json(audio_status))

                except Exception as e:
                    print(f"[WS] Error processing message: {e}")

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            sender.cancel()
            self.clients.pop(websocket, None)
            if session.wants_audio:
                await self._update_audio()
            print(f"[WS] Client disconnected {peer}. Remaining: {len(self.clients)}")

    async def _update_audio(self, device_id: Optional[int] = None):
        """Keeps the microphone open only while some client is listening.
        A client that names a different microphone switches it for everyone."""
        wanted = any(s.wants_audio for s in self.clients.values())
        async with self._device_lock:
            if not wanted:
                if self.audio_source.is_enabled():
                    await asyncio.to_thread(self.audio_source.stop)
            elif (not self.audio_source.is_enabled()
                  or (device_id is not None and device_id != self.audio_source.device_id)):
                await asyncio.to_thread(self.audio_source.start, device_id)

    def _pack_json(self, obj: Dict[str, Any]) -> bytes:
        payload = json.dumps(obj).encode("utf-8")
        return bytes([PKT_JSON]) + payload

    def _build_device_list_msg(self, force_refresh: bool = False) -> Dict[str, Any]:
        cams = self.camera_source.list_devices(force_refresh=force_refresh)
        mics = self.audio_source.list_devices()
        msg = {
            "type": "device_list",
            "cameras": cams,
            "microphones": mics,
            "current_camera": self.camera_source.get_info(),
            "audio_enabled": self.audio_source.is_enabled()
        }
        if not cams and sys.platform == "darwin":
            msg["camera_hint"] = MACOS_CAMERA_HINT
        return msg

    def broadcast(self, data: bytes):
        for session in self.clients.values():
            session.push_control(data)

    async def stream_loop(self):
        last_frame_ts = 0.0

        while self._running:
            # Check video frame
            frame_item = self.camera_source.read_frame()
            if frame_item is not None:
                capture_ts, rgb_frame = frame_item
                # Only broadcast new frames
                if capture_ts != last_frame_ts and len(self.clients) > 0:
                    last_frame_ts = capture_ts

                    # ctypes drops the GIL, so encoding in a worker thread keeps the
                    # event loop free for control messages and audio
                    encode_res = await asyncio.to_thread(self.encoder.encode, rgb_frame)
                    if encode_res is not None:
                        phf_bytes, enc_ms, raw_sz, phf_sz = encode_res

                        # Build binary packet:
                        # [1 byte: PKT_VIDEO] + [8 bytes: capture_ts double LE] + [4 bytes: enc_ms float LE] + [phf_bytes]
                        hdr = struct.pack("<Bdf", PKT_VIDEO, capture_ts, enc_ms)
                        packet = hdr + phf_bytes
                        for session in self.clients.values():
                            session.push_video(packet)

                        self._fps_counter += 1
                        now = time.perf_counter()
                        dt = now - self._fps_last_time
                        if dt >= 1.0:
                            self._measured_fps = self._fps_counter / dt
                            self._fps_counter = 0
                            self._fps_last_time = now

            # Check audio chunk
            if self.audio_source.is_enabled():
                listeners = [s for s in self.clients.values() if s.wants_audio]
                chunk = self.audio_source.get_chunk()
                while chunk is not None:
                    # 2-byte header ensures the 16-bit PCM payload is strictly 2-byte aligned in TypedArray
                    audio_pkt = bytes([PKT_AUDIO, 0x00]) + chunk
                    for session in listeners:
                        session.push_audio(audio_pkt)
                    chunk = self.audio_source.get_chunk()

            await asyncio.sleep(0.002)

    async def run(self):
        self._running = True
        self._device_lock = asyncio.Lock()
        self.start_http()

        # Start default camera at lowest verified resolution
        devices = self.camera_source.list_devices()
        if devices:
            default_id = devices[0]["id"]
            default_res = devices[0].get("default_resolution", [320, 240])
            lowest_w, lowest_h = default_res
            print(f"[Camera] Starting default camera #{default_id} at {lowest_w}x{lowest_h}...")
            self.camera_source.start(default_id, lowest_w, lowest_h, 30)
        else:
            print("[Camera] No camera detected on system.")
            if sys.platform == "darwin":
                print(f"[Camera] {MACOS_CAMERA_HINT}")

        print(f"[WS] WebSocket streaming server listening on ws://localhost:{self.ws_port}")

        # compression=None: PHF frames are already entropy-coded, and per-message deflate
        # spent ~28 ms of event-loop time per 1080p frame per client to save ~5%
        async with ws_serve(self.handle_client, self.host, self.ws_port,
                            process_request=self._check_origin,
                            compression=None):
            await self.stream_loop()

    def stop(self):
        self._running = False
        self.camera_source.stop()
        self.audio_source.stop()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
