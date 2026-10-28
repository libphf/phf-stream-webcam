import os
import ctypes
import time
from typing import Optional, Tuple
import numpy as np

class PHFEncoder:
    def __init__(self, lib_path: Optional[str] = None):
        self._lib = None
        self._load_library(lib_path)

    def _load_library(self, lib_path: Optional[str]):
        search_paths = []
        if lib_path:
            search_paths.append(lib_path)
            
        project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        bin_dir = os.path.join(project_dir, "bin")
        root = os.path.abspath(os.path.join(project_dir, ".."))
        
        search_paths.extend([
            # 1. Project-local bin/ directory (self-contained standalone)
            os.path.join(bin_dir, "phf.dll"),
            os.path.join(bin_dir, "libphf.so"),
            os.path.join(bin_dir, "libphf.dylib"),
            os.path.join(bin_dir, "windows_x64", "phf.dll"),
            os.path.join(bin_dir, "linux_x86_64", "libphf.so"),
            os.path.join(bin_dir, "macos_arm64", "libphf.dylib"),
            # 2. Monorepo development fallback
            os.path.join(root, "phf", "libphf", "phf.dll"),
            os.path.join(root, "phf", "libphf", "libphf.so"),
            os.path.join(root, "phf", "libphf", "libphf.dylib"),
            os.path.join(root, "phf", "phf.dll"),
            # 3. Server directory fallback
            os.path.join(os.path.dirname(__file__), "phf.dll"),
            os.path.join(os.path.dirname(__file__), "libphf.so"),
        ])
        
        for p in search_paths:
            if os.path.isfile(p):
                try:
                    self._lib = ctypes.CDLL(p)
                    # print(f"[PHFEncoder] Successfully loaded native library: {p}")
                    break
                except Exception as e:
                    print(f"[PHFEncoder] Failed to load {p}: {e}")
                    
        if not self._lib:
            raise RuntimeError(f"Could not load libphf library. Searched: {search_paths}")
            
        # Bind C prototypes
        self._lib.phf_encode_memory.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint8,
            ctypes.POINTER(ctypes.c_size_t)
        ]
        self._lib.phf_encode_memory.restype = ctypes.POINTER(ctypes.c_uint8)
        
        self._lib.phf_free.argtypes = [ctypes.c_void_p]
        self._lib.phf_free.restype = None

    def encode(self, rgb_frame: np.ndarray) -> Optional[Tuple[bytes, float, int, int]]:
        """
        Encodes an HxWx3 uint8 RGB numpy array into a PHF binary stream.
        Returns: (phf_bytes, encode_time_ms, raw_size, phf_size)
        """
        if rgb_frame is None or rgb_frame.ndim != 3:
            return None
            
        h, w, ch = rgb_frame.shape
        if ch != 3 and ch != 4:
            return None
            
        if not rgb_frame.flags['C_CONTIGUOUS']:
            rgb_frame = np.ascontiguousarray(rgb_frame)
            
        raw_size = w * h * ch
        out_sz = ctypes.c_size_t(0)
        
        pixels_ptr = rgb_frame.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))
        
        t0 = time.perf_counter()
        ptr = self._lib.phf_encode_memory(pixels_ptr, w, h, ch, ctypes.byref(out_sz))
        dt_ms = (time.perf_counter() - t0) * 1000.0
        
        if not ptr:
            return None
            
        phf_bytes = ctypes.string_at(ptr, out_sz.value)
        self._lib.phf_free(ptr)
        
        return (phf_bytes, dt_ms, raw_size, len(phf_bytes))
