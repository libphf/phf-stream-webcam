# PHF Stream Webcam — Real-Time Lossless Webcam & Audio

Next-generation real-time webcam and audio streaming server powered by the **Pure High-Fidelity Format (PHF)**, WebSockets, and WebAssembly SIMD128.

Unlike lossy streaming protocols (MJPEG, H.264 WebRTC) that introduce 4:2:0 chroma subsampling, mosquito noise, and compression block artifacts, **PHF streams every camera frame 100% mathematically lossless**.

---

## ⚡ Architecture

```text
phf-stream-webcam/
├── bin/                      # Pre-built native dynamic libraries (self-contained)
│   ├── phf.dll               # Pre-compiled Windows x64 native engine
│   └── README.md             # Compilation instructions for Linux / macOS
├── server/
│   ├── base_source.py        # Abstract base class for video capture sources
│   ├── camera_source.py      # Multiplatform camera enumeration & capture backend
│   ├── audio_source.py       # sounddevice microphone capture (16-bit PCM)
│   ├── encoder.py            # High-speed PHF ctypes compression wrapper (libphf)
│   └── stream_server.py      # Multi-client HTTP + WebSocket streaming hub
├── web/
│   ├── index.html            # Web streaming dashboard with live HUD & audio controls
│   ├── style.css             # Glassmorphic dark theme
│   ├── stream-client.js      # WebSocket client, Web Audio API player, and WASM bridge
│   ├── phf-view.js           # PHF WebAssembly loader
│   ├── phf.js                # Emscripten runtime
│   └── phf.wasm              # SIMD128 PHF decoder
├── requirements.txt          # Python dependencies
├── run.py                    # Single-command launcher
└── README.md
```

---

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Launch the Stream Server
```bash
python run.py
```

Optional parameters:
```bash
python run.py --http-port 8080 --ws-port 8765
```

### 3. Open in Browser
👉 **`http://localhost:8080`**

---

## 💎 Features

* **Self-Contained & Ready to Run:** Includes pre-compiled native binaries (`bin/phf.dll` for Windows x64) and pre-built WebAssembly SIMD128 decoder (`web/phf.wasm`). No manual compiler setup required for testing.
* **Instant Multiplatform Camera Discovery:** Scans all connected cameras on the host system with clear notifications if no camera is detected.
* **Smart Resolution Selection:** Defaults to the verified baseline resolution (`320x240`) for instant playback, with dynamic switching to `640x480 (VGA)`, `800x600 (SVGA)`, `1280x720`, and `1920x1080`, complete with hardware bandwidth warnings when high resolutions cause USB 2.0 sensor throttling.
* **Deadlock-Free Threading:** Robust thread isolation ensures smooth resolution switching without DirectShow driver deadlocks.
* **Synchronized Audio Streaming:** Real-time microphone capture (16-bit PCM) with in-browser Web Audio API playback, deduplicated physical hardware detection, dynamic volume boost (up to 300%), and a real-time VU meter.
* **Zero-Allocation Canvas Renderer:** Fast direct SIMD memory copy onto an HTML5 canvas avoiding GC pressure.
* **Live HUD Overlay:** Real-time metrics displaying FPS, capture-to-render latency (ms), bitrate (Mbps), decode time (ms), and lossless compression savings.

---

## 📜 License
MIT License. Part of the [PHF (libphf)](https://github.com/libphf/phf) ecosystem.
