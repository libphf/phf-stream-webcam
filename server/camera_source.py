import cv2
import time
import threading
import sys
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
from base_source import BaseVideoSource

KNOWN_RESOLUTIONS = [
    {"width": 320,  "height": 240,  "tier": "smooth",           "label": "320x240 (Recomendada / Padrao)"},
    {"width": 640,  "height": 480,  "tier": "smooth",           "label": "640x480 (VGA)"},
    {"width": 800,  "height": 600,  "tier": "moderate",         "label": "800x600 (SVGA)"},
    {"width": 1280, "height": 720,  "tier": "hardware_limited", "label": "1280x720 (Alerta: Hardware Limitado)"},
    {"width": 1920, "height": 1080, "tier": "hardware_limited", "label": "1920x1080 (Alerta: Hardware Limitado)"}
]

def get_platform_backend():
    if sys.platform == 'win32':
        return cv2.CAP_DSHOW
    elif sys.platform.startswith('linux'):
        return cv2.CAP_V4L2
    elif sys.platform == 'darwin':
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY

class CameraSource(BaseVideoSource):
    def __init__(self):
        self._cap: Optional[cv2.VideoCapture] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_ts: float = 0.0
        self._frame_count: int = 0
        
        self._device_id: int = 0
        self._width: int = 320
        self._height: int = 240
        self._fps: int = 30
        self._actual_fps: float = 0.0
        self._cached_devices: Optional[List[Dict[str, Any]]] = None

    def list_devices(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        if self._cached_devices is not None and not force_refresh:
            return self._cached_devices

        devices = []
        backend = get_platform_backend()
        
        # Fast non-blocking device detection (check index 0)
        cap = cv2.VideoCapture(0, backend)
        if cap.isOpened():
            # Quick check if open
            devices.append({
                "id": 0,
                "name": "Camera 0 (Default)",
                "default_resolution": [320, 240],
                "resolutions": KNOWN_RESOLUTIONS
            })
            cap.release()
            
            # Check if second camera exists
            cap2 = cv2.VideoCapture(1, backend)
            if cap2.isOpened():
                devices.append({
                    "id": 1,
                    "name": "Camera 1",
                    "default_resolution": [320, 240],
                    "resolutions": KNOWN_RESOLUTIONS
                })
                cap2.release()

        self._cached_devices = devices
        return devices

    def start(self, device_id: Any, width: int = 320, height: int = 240, fps: int = 30) -> Tuple[bool, Optional[str]]:
        try:
            dev_idx = int(device_id)
        except (ValueError, TypeError):
            dev_idx = 0

        # Cleanly stop running thread and release webcam before configuring new resolution
        self.stop()
        time.sleep(0.15) # Safe release window for DirectShow driver

        backend = get_platform_backend()
        cap = cv2.VideoCapture(dev_idx, backend)
        if not cap.isOpened():
            return False, f"Falha ao abrir a câmera #{dev_idx}."
            
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        
        self._cap = cap
        self._device_id = dev_idx
        self._width = width
        self._height = height
        self._fps = fps
        self._running = True
        self._frame_count = 0
        
        # Dedicated capture thread (sole caller of cap.read)
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

        warning = None
        if width > 800:
            warning = (f"Aviso de Hardware: A resolução {width}x{height} causa queda brusca de taxa de quadros "
                       f"neste modelo de câmera (gargalo do sensor/USB). Recomendamos 640x480 ou 320x240.")
                       
        return True, warning

    def _capture_loop(self):
        last_calc_time = time.perf_counter()
        calc_frames = 0
        
        while self._running:
            cap = self._cap
            if cap is None or not cap.isOpened():
                time.sleep(0.01)
                continue

            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.005)
                continue
                
            ts = time.time()
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            with self._lock:
                self._latest_frame = np.ascontiguousarray(rgb)
                self._latest_ts = ts
                self._frame_count += 1
                calc_frames += 1
                
            now = time.perf_counter()
            dt = now - last_calc_time
            if dt >= 1.0:
                self._actual_fps = calc_frames / dt
                calc_frames = 0
                last_calc_time = now

    def read_frame(self) -> Optional[Tuple[float, np.ndarray]]:
        with self._lock:
            if self._latest_frame is None:
                return None
            return (self._latest_ts, self._latest_frame)

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
            self._thread = None
            
        if self._cap:
            self._cap.release()
            self._cap = None
            
        with self._lock:
            self._latest_frame = None

    def get_info(self) -> Dict[str, Any]:
        return {
            "device_id": self._device_id,
            "width": self._width,
            "height": self._height,
            "fps_target": self._fps,
            "fps_actual": round(self._actual_fps, 1),
            "frame_count": self._frame_count,
            "running": self._running
        }
