import sounddevice as sd
import numpy as np
import threading
import queue
from typing import List, Dict, Any, Optional

DEFAULT_SAMPLE_RATE = 44100
DEFAULT_CHANNELS = 1 # Mono 16-bit PCM for efficient streaming
BLOCK_SIZE = 1024

class AudioSource:
    def __init__(self):
        self._stream: Optional[sd.InputStream] = None
        self._enabled = False
        self._device_id: Optional[int] = None
        self._queue = queue.Queue(maxsize=50)
        self._lock = threading.Lock()

    def list_devices(self) -> List[Dict[str, Any]]:
        devs = []
        try:
            # Query only the default HostAPI to avoid listing the same physical device
            # across MME, DirectSound, WASAPI and WDM-KS backends
            default_api = sd.default.hostapi
            host_info = sd.query_hostapis(default_api)
            
            # Build a lookup of full names from all devices to expand MME 31-char truncated names
            all_devices = sd.query_devices()
            full_names = {}
            for d in all_devices:
                fn = d.get('name', '')
                if '(' in fn and ')' in fn:
                    tag = fn[fn.find('(')+1:fn.rfind(')')].strip().lower()
                    if len(tag) > 3 and (tag not in full_names or len(fn) > len(full_names[tag])):
                        full_names[tag] = fn

            camera_mics = []
            other_mics = []

            for dev_idx in host_info['devices']:
                d = sd.query_devices(dev_idx)
                if d.get('max_input_channels', 0) > 0:
                    raw_name = d.get('name', f"Microphone {dev_idx}")
                    low = raw_name.lower()
                    
                    # Skip virtual system routing mappers
                    if any(skip in low for skip in ['mapeador', 'sound mapper', 'driver de captura', 'primary sound']):
                        continue

                    # Try to expand truncated name
                    clean_name = raw_name
                    if '(' in raw_name:
                        tag = raw_name[raw_name.find('(')+1:].replace(')', '').strip().lower()
                        for k, v in full_names.items():
                            if k.startswith(tag[:10]) or tag.startswith(k[:10]):
                                clean_name = v
                                break

                    dev_entry = {
                        "id": dev_idx,
                        "name": clean_name,
                        "channels": d.get('max_input_channels', 1),
                        "default_samplerate": int(d.get('default_samplerate', DEFAULT_SAMPLE_RATE))
                    }

                    if "camera" in clean_name.lower() or "webcam" in clean_name.lower():
                        camera_mics.append(dev_entry)
                    else:
                        other_mics.append(dev_entry)

            # Prioritize camera mic first
            devs = camera_mics + other_mics
        except Exception as e:
            print(f"[Audio] Error querying devices: {e}")
        return devs

    def _audio_callback(self, indata, frames, time_info, status):
        if not self._enabled:
            return
        # indata is int16 array shape (frames, channels)
        pcm_bytes = indata.tobytes()
        try:
            self._queue.put_nowait(pcm_bytes)
        except queue.Full:
            # Drop oldest chunk to prevent buffer bloat
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(pcm_bytes)
            except queue.Empty:
                pass

    def start(self, device_id: Optional[int] = None) -> bool:
        self.stop()
        self._device_id = device_id
        
        try:
            self._stream = sd.InputStream(
                device=device_id,
                channels=DEFAULT_CHANNELS,
                samplerate=DEFAULT_SAMPLE_RATE,
                dtype='int16',
                blocksize=BLOCK_SIZE,
                callback=self._audio_callback
            )
            self._stream.start()
            self._enabled = True
            return True
        except Exception as e:
            print(f"[Audio] Failed to start audio input: {e}")
            self._stream = None
            self._enabled = False
            return False

    def get_chunk(self) -> Optional[bytes]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        if not enabled:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def is_enabled(self) -> bool:
        return self._enabled

    def stop(self):
        self._enabled = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
