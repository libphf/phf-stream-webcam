import os
import sys
import json
import time
import struct
import asyncio
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Set, Dict, Any, Optional

import websockets
from websockets.asyncio.server import serve as ws_serve

from camera_source import CameraSource
from audio_source import AudioSource
from encoder import PHFEncoder

# Packet types
PKT_VIDEO = 0x01
PKT_AUDIO = 0x02
PKT_JSON  = 0x03

class NoCacheHTTPHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format, *args):
        # Silence static file logs for cleaner console
        pass

class StreamServer:
    def __init__(self, web_dir: str, http_port: int = 8080, ws_port: int = 8765):
        self.web_dir = web_dir
        self.http_port = http_port
        self.ws_port = ws_port
        
        self.camera_source = CameraSource()
        self.audio_source = AudioSource()
        self.encoder = PHFEncoder()
        
        self.clients: Set[Any] = set()
        self._running = False
        self._httpd: Optional[ThreadingHTTPServer] = None
        
        # Performance trackers
        self._fps_counter = 0
        self._fps_last_time = time.perf_counter()
        self._measured_fps = 0.0

    def start_http(self):
        handler = lambda *args, **kwargs: NoCacheHTTPHandler(*args, directory=self.web_dir, **kwargs)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.http_port), handler)
        http_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        http_thread.start()
        print(f"[HTTP] Web dashboard serving at: http://localhost:{self.http_port}")

    async def handle_client(self, websocket):
        self.clients.add(websocket)
        peer = websocket.remote_address
        print(f"[WS] Client connected from {peer}. Active clients: {len(self.clients)}")
        
        try:
            # Send initial device list and state to newly connected client
            init_msg = self._build_device_list_msg()
            await websocket.send(self._pack_json(init_msg))
            
            async for raw_msg in websocket:
                try:
                    data = json.loads(raw_msg)
                    action = data.get("action")
                    
                    if action == "get_devices":
                        force = bool(data.get("refresh", False))
                        msg = self._build_device_list_msg(force_refresh=force)
                        await websocket.send(self._pack_json(msg))
                        
                    elif action == "set_camera":
                        cam_id = data.get("id", 0)
                        w = data.get("width", 320)
                        h = data.get("height", 240)
                        fps = data.get("fps", 30)
                        print(f"[Control] Setting camera {cam_id} ({w}x{h} @ {fps}fps)")
                        ok, warning = self.camera_source.start(cam_id, w, h, fps)
                        status_msg = {
                            "type": "camera_status",
                            "success": ok,
                            "warning": warning,
                            "info": self.camera_source.get_info()
                        }
                        await self.broadcast(self._pack_json(status_msg))
                        
                    elif action == "toggle_audio":
                        enabled = bool(data.get("enabled", True))
                        mic_id = data.get("device_id")
                        print(f"[Control] Audio enabled: {enabled} (Mic: {mic_id})")
                        if enabled and not self.audio_source.is_enabled():
                            self.audio_source.start(mic_id)
                        elif not enabled:
                            self.audio_source.set_enabled(False)
                            
                        audio_status = {
                            "type": "audio_status",
                            "enabled": self.audio_source.is_enabled()
                        }
                        await self.broadcast(self._pack_json(audio_status))
                        
                except Exception as e:
                    print(f"[WS] Error processing message: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            print(f"[WS] Client disconnected {peer}. Remaining: {len(self.clients)}")

    def _pack_json(self, obj: Dict[str, Any]) -> bytes:
        payload = json.dumps(obj).encode("utf-8")
        return bytes([PKT_JSON]) + payload

    def _build_device_list_msg(self, force_refresh: bool = False) -> Dict[str, Any]:
        cams = self.camera_source.list_devices(force_refresh=force_refresh)
        mics = self.audio_source.list_devices()
        return {
            "type": "device_list",
            "cameras": cams,
            "microphones": mics,
            "current_camera": self.camera_source.get_info(),
            "audio_enabled": self.audio_source.is_enabled()
        }

    async def broadcast(self, data: bytes):
        if not self.clients:
            return
        to_remove = set()
        for client in list(self.clients):
            try:
                await client.send(data)
            except Exception:
                to_remove.add(client)
        self.clients.difference_update(to_remove)

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
                    
                    encode_res = self.encoder.encode(rgb_frame)
                    if encode_res is not None:
                        phf_bytes, enc_ms, raw_sz, phf_sz = encode_res
                        
                        # Build binary packet:
                        # [1 byte: PKT_VIDEO] + [8 bytes: capture_ts double LE] + [4 bytes: enc_ms float LE] + [phf_bytes]
                        hdr = struct.pack("<Bdf", PKT_VIDEO, capture_ts, enc_ms)
                        packet = hdr + phf_bytes
                        await self.broadcast(packet)
                        
                        self._fps_counter += 1
                        now = time.perf_counter()
                        dt = now - self._fps_last_time
                        if dt >= 1.0:
                            self._measured_fps = self._fps_counter / dt
                            self._fps_counter = 0
                            self._fps_last_time = now

            # Check audio chunk
            if self.audio_source.is_enabled() and len(self.clients) > 0:
                chunk = self.audio_source.get_chunk()
                while chunk is not None:
                    # 2-byte header ensures the 16-bit PCM payload is strictly 2-byte aligned in TypedArray
                    audio_pkt = bytes([PKT_AUDIO, 0x00]) + chunk
                    await self.broadcast(audio_pkt)
                    chunk = self.audio_source.get_chunk()

            await asyncio.sleep(0.002)

    async def run(self):
        self._running = True
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
            
        print(f"[WS] WebSocket streaming server listening on ws://localhost:{self.ws_port}")
        
        async with ws_serve(self.handle_client, "0.0.0.0", self.ws_port):
            await self.stream_loop()

    def stop(self):
        self._running = False
        self.camera_source.stop()
        self.audio_source.stop()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
