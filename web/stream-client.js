import { getPHFModule } from './phf-view.js?v=1.0.2';

// Protocol Packet IDs
const PKT_VIDEO = 0x01;
const PKT_AUDIO = 0x02;
const PKT_JSON  = 0x03;

let ws = null;
let phfMod = null;
let wsPort = 8765; // replaced by the server's /config.json (follows --ws-port)

// Video Canvas
const canvas = document.getElementById('videoCanvas');
const ctx = canvas.getContext('2d');
const loadingOverlay = document.getElementById('loadingOverlay');
const loadingText = document.getElementById('loadingText');
const connStatus = document.getElementById('connStatus');

// HUD elements
const hudFps = document.getElementById('hudFps');
const hudLatency = document.getElementById('hudLatency');
const hudBitrate = document.getElementById('hudBitrate');
const hudDecodeTime = document.getElementById('hudDecodeTime');
const hudRatio = document.getElementById('hudRatio');

// Control elements
const cameraSelect = document.getElementById('cameraSelect');
const resSelect = document.getElementById('resSelect');
const btnApplyCamera = document.getElementById('btnApplyCamera');

const micSelect = document.getElementById('micSelect');
const btnToggleAudio = document.getElementById('btnToggleAudio');
const btnAudioText = document.getElementById('btnAudioText');
const volSlider = document.getElementById('volSlider');
const vuBar = document.getElementById('vuBar');

// Audio Context (Web Audio API)
let audioCtx = null;
let gainNode = null;
let nextPlayTime = 0;
let isAudioEnabled = false;

// Metrics tracking
let frameCounter = 0;
let bytesCounter = 0;
let lastFpsTime = performance.now();

// Persistent memory buffers for WASM zero-allocation loop
let wasmSrcPtr = 0;
let wasmSrcCapacity = 0;
let wasmDstPtr = 0;
let wasmDstCapacity = 0;
let cachedImgData = null;

function setConnectionState(state, text) {
    const dot = connStatus.querySelector('.dot');
    const label = connStatus.querySelector('.text');
    dot.className = `dot ${state}`;
    label.innerText = text;
}

// ---------------------------------------------------------------------------
// Audio Pipeline (Web Audio API 16-bit PCM streaming)
// ---------------------------------------------------------------------------
function initAudio() {
    if (!audioCtx) {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        audioCtx = new AudioContext({ sampleRate: 44100 });
        gainNode = audioCtx.createGain();
        gainNode.gain.value = parseFloat(volSlider.value);
        gainNode.connect(audioCtx.destination);
    }
    if (audioCtx.state === 'suspended') {
        audioCtx.resume();
    }
}

volSlider.addEventListener('input', (e) => {
    if (gainNode) {
        gainNode.gain.value = parseFloat(e.target.value);
    }
});

function playPcmChunk(u8Packet) {
    if (!isAudioEnabled || !audioCtx) return;

    // The packet layout has a 2-byte header: [PKT_AUDIO (0x02), 0x00]
    // Slicing starting at offset 2 guarantees a clean, strictly 2-byte aligned buffer
    const pcmBuf = u8Packet.buffer.slice(u8Packet.byteOffset + 2, u8Packet.byteOffset + u8Packet.byteLength);
    const int16 = new Int16Array(pcmBuf);
    const n = int16.length;
    if (n === 0) return;

    // Convert int16 to float32 [-1.0, 1.0] and compute VU meter level
    const float32 = new Float32Array(n);
    let sumSquares = 0;
    for (let i = 0; i < n; i++) {
        const val = int16[i] / 32768.0;
        float32[i] = val;
        sumSquares += val * val;
    }

    const rms = Math.sqrt(sumSquares / n);
    const vuPercent = Math.min(100, Math.round(rms * 500));
    vuBar.style.width = `${vuPercent}%`;

    const audioBuf = audioCtx.createBuffer(1, n, 44100);
    audioBuf.copyToChannel(float32, 0);

    const source = audioCtx.createBufferSource();
    source.buffer = audioBuf;
    source.connect(gainNode);

    const now = audioCtx.currentTime;
    if (nextPlayTime < now || nextPlayTime > now + 0.25) {
        nextPlayTime = now + 0.02; // Small 20ms jitter buffer
    }
    source.start(nextPlayTime);
    nextPlayTime += audioBuf.duration;
}

// ---------------------------------------------------------------------------
// Video Pipeline (WASM SIMD128 PHF Frame Decoder)
// ---------------------------------------------------------------------------
function handleVideoPacket(u8) {
    if (!phfMod) return;

    // Packet header layout:
    // Offset 0: 0x01 (PKT_VIDEO)
    // Offset 1..8: capture_ts (Float64 / double LE)
    // Offset 9..12: enc_ms (Float32 LE)
    // Offset 13..end: PHF raw bytes
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const captureTsSec = dv.getFloat64(1, true);
    const encMs = dv.getFloat32(9, true);

    const phfBytes = u8.subarray(13);
    const phfLen = phfBytes.byteLength;
    if (phfLen < 20) return;

    // Parse PHF header: width at 8, height at 12
    const width = dv.getUint32(13 + 8, true);
    const height = dv.getUint32(13 + 12, true);
    const channels = dv.getUint8(13 + 5);

    if (width === 0 || height === 0) return;

    // Adjust canvas resolution if changed
    if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
    }

    // Allocate / resize WASM input buffer
    if (wasmSrcCapacity < phfLen) {
        if (wasmSrcPtr) phfMod._free(wasmSrcPtr);
        wasmSrcCapacity = Math.max(phfLen * 2, 65536);
        wasmSrcPtr = phfMod._malloc(wasmSrcCapacity);
        if (!wasmSrcPtr) {
            wasmSrcCapacity = 0;
            console.error('[PHF] Out of WASM memory for input buffer');
            return;
        }
    }
    phfMod.HEAPU8.set(phfBytes, wasmSrcPtr);

    const outBytes = width * height * 4;
    // Allocate / resize WASM output buffer (RGBA)
    if (wasmDstCapacity < outBytes) {
        if (wasmDstPtr) phfMod._free(wasmDstPtr);
        wasmDstCapacity = Math.max(outBytes * 2, 1048576);
        wasmDstPtr = phfMod._malloc(wasmDstCapacity);
        if (!wasmDstPtr) {
            wasmDstCapacity = 0;
            console.error('[PHF] Out of WASM memory for output buffer');
            return;
        }
    }

    const t0 = performance.now();
    // Decode into RGBA with stride = 4
    const rc = phfMod._phf_decode_memory(wasmSrcPtr, phfLen, wasmDstPtr, width, height, 4);
    const decMs = performance.now() - t0;

    if (rc === 0) {
        if (!cachedImgData || cachedImgData.width !== width || cachedImgData.height !== height) {
            cachedImgData = ctx.createImageData(width, height);
        }
        cachedImgData.data.set(new Uint8Array(phfMod.HEAPU8.buffer, wasmDstPtr, outBytes));
        ctx.putImageData(cachedImgData, 0, 0);

        loadingOverlay.classList.add('hidden');

        // Latency: current wall clock vs capture wall clock
        const totalLatencyMs = (Date.now() / 1000.0 - captureTsSec) * 1000.0;

        frameCounter++;
        bytesCounter += phfLen;

        // Update HUD
        hudLatency.innerText = `${Math.max(0, Math.round(totalLatencyMs))} ms`;
        hudDecodeTime.innerText = `${decMs.toFixed(1)} ms`;

        const rawBytes = width * height * channels;
        const ratio = (rawBytes / phfLen).toFixed(2);
        const savings = ((1.0 - phfLen / rawBytes) * 100).toFixed(0);
        hudRatio.innerText = `${ratio}x (-${savings}%)`;
    } else {
        console.error(`[PHF] Decode failed with error code ${rc}`);
    }
}

// ---------------------------------------------------------------------------
// WebSocket Networking Hub
// ---------------------------------------------------------------------------
function connectWebSocket() {
    setConnectionState('connecting', 'Connecting...');
    const wsHost = window.location.hostname || 'localhost';
    const wsUrl = `ws://${wsHost}:${wsPort}`;

    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
        setConnectionState('connected', 'Live Stream Active');
        loadingText.innerText = 'Receiving PHF video stream...';
    };

    ws.onmessage = (event) => {
        if (typeof event.data === 'string') {
            try {
                handleJsonMessage(JSON.parse(event.data));
            } catch (e) {
                console.error(e);
            }
            return;
        }

        const u8 = new Uint8Array(event.data);
        if (u8.length === 0) return;

        const pktType = u8[0];
        if (pktType === PKT_VIDEO) {
            handleVideoPacket(u8);
        } else if (pktType === PKT_AUDIO) {
            playPcmChunk(u8);
        } else if (pktType === PKT_JSON) {
            const jsonStr = new TextDecoder().decode(u8.subarray(1));
            try {
                handleJsonMessage(JSON.parse(jsonStr));
            } catch (e) {
                console.error(e);
            }
        }
    };

    ws.onclose = () => {
        setConnectionState('error', 'Disconnected');
        loadingOverlay.classList.remove('hidden');
        loadingText.innerText = 'Connection lost. Reconnecting in 2s...';
        setTimeout(connectWebSocket, 2000);
    };

    ws.onerror = () => {
        setConnectionState('error', 'Connection Error');
    };
}

// Hardware alert banner elements
const alertBanner = document.getElementById('alertBanner');
const alertMsg = document.getElementById('alertMsg');
const btnCloseAlert = document.getElementById('btnCloseAlert');
const btnRefreshCameras = document.getElementById('btnRefreshCameras');
const noCameraAlert = document.getElementById('noCameraAlert');

btnCloseAlert.addEventListener('click', () => {
    alertBanner.classList.add('hidden');
});

btnRefreshCameras.addEventListener('click', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        cameraSelect.innerHTML = '<option value="">Scanning cameras...</option>';
        resSelect.innerHTML = '<option value="">Checking hardware...</option>';
        ws.send(JSON.stringify({ action: 'get_devices', refresh: true }));
    }
});

let cachedCameras = [];

function updateResolutionOptions(camId) {
    resSelect.innerHTML = '';
    const cam = cachedCameras.find(c => c.id == camId) || cachedCameras[0];
    if (cam && cam.resolutions && cam.resolutions.length > 0) {
        cam.resolutions.forEach((r, i) => {
            const opt = document.createElement('option');
            opt.value = `${r.width}x${r.height}`;
            opt.innerText = r.label || `${r.width}x${r.height}`;
            if (r.tier === 'hardware_limited') {
                opt.style.color = '#e3b341';
            }
            if (i === 0) opt.selected = true; // Select lowest verified resolution by default
            resSelect.appendChild(opt);
        });
    } else {
        resSelect.innerHTML = '<option value="320x240" selected>320x240 - Default</option>';
    }
}

cameraSelect.addEventListener('change', () => {
    updateResolutionOptions(cameraSelect.value);
});

function handleJsonMessage(msg) {
    if (msg.type === 'device_list') {
        cachedCameras = msg.cameras || [];

        // Check if cameras were found
        cameraSelect.innerHTML = '';
        if (cachedCameras.length > 0) {
            noCameraAlert.classList.add('hidden');
            cachedCameras.forEach(cam => {
                const opt = document.createElement('option');
                opt.value = cam.id;
                opt.innerText = `${cam.name}`;
                cameraSelect.appendChild(opt);
            });
            if (msg.current_camera) {
                cameraSelect.value = msg.current_camera.device_id;
            }
            updateResolutionOptions(cameraSelect.value);
        } else {
            if (msg.camera_hint) {
                noCameraAlert.querySelector('span').innerText = `⚠️ ${msg.camera_hint}`;
            }
            noCameraAlert.classList.remove('hidden');
            cameraSelect.innerHTML = '<option value="">Nenhuma câmera detectada</option>';
            resSelect.innerHTML = '<option value="">Indisponível</option>';
            loadingText.innerText = 'Nenhuma câmera conectada ao computador.';
        }

        // Populate microphones
        micSelect.innerHTML = '';
        if (msg.microphones && msg.microphones.length > 0) {
            msg.microphones.forEach(mic => {
                const opt = document.createElement('option');
                opt.value = mic.id;
                opt.innerText = `${mic.name}`;
                micSelect.appendChild(opt);
            });
        } else {
            micSelect.innerHTML = '<option value="">Nenhum microfone detectado</option>';
        }
    } else if (msg.type === 'camera_status') {
        if (msg.warning) {
            alertMsg.innerText = msg.warning;
            alertBanner.classList.remove('hidden');
        } else {
            alertBanner.classList.add('hidden');
        }
        if (!msg.success) {
            alertMsg.innerText = 'Erro: A câmera não conseguiu iniciar nesta configuração.';
            alertBanner.classList.remove('hidden');
        }
    } else if (msg.type === 'audio_status') {
        if (isAudioEnabled && !msg.enabled) {
            alertMsg.innerText = 'Erro: o microfone não pôde ser aberto.';
            alertBanner.classList.remove('hidden');
            setAudioButton(false);
        }
    }
}

// ---------------------------------------------------------------------------
// Controls Interaction
// ---------------------------------------------------------------------------
btnApplyCamera.addEventListener('click', () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const camId = parseInt(cameraSelect.value) || 0;
    const [w, h] = resSelect.value.split('x').map(x => parseInt(x));

    loadingOverlay.classList.remove('hidden');
    loadingText.innerText = `Switching camera to ${w}x${h}...`;

    ws.send(JSON.stringify({
        action: 'set_camera',
        id: camId,
        width: w,
        height: h,
        fps: 60 // Max rate supported by hardware
    }));
});

function setAudioButton(enabled) {
    isAudioEnabled = enabled;
    if (enabled) {
        btnToggleAudio.className = 'btn btn-toggle audio-on';
        btnToggleAudio.querySelector('.icon').innerText = '🎙️';
        btnAudioText.innerText = 'Audio Streaming (Live)';
    } else {
        btnToggleAudio.className = 'btn btn-toggle audio-off';
        btnToggleAudio.querySelector('.icon').innerText = '🔇';
        btnAudioText.innerText = 'Enable Audio';
        vuBar.style.width = '0%';
    }
}

btnToggleAudio.addEventListener('click', async () => {
    initAudio();
    if (audioCtx && audioCtx.state === 'suspended') {
        try {
            await audioCtx.resume();
        } catch (e) {
            console.error('AudioContext resume failed:', e);
        }
    }
    setAudioButton(!isAudioEnabled);

    if (ws && ws.readyState === WebSocket.OPEN) {
        // null lets the server pick the system default microphone
        const micId = micSelect.value === '' ? null : parseInt(micSelect.value);
        ws.send(JSON.stringify({
            action: 'toggle_audio',
            enabled: isAudioEnabled,
            device_id: micId
        }));
    }
});

// Periodic FPS & Bitrate calculation
setInterval(() => {
    const now = performance.now();
    const dt = (now - lastFpsTime) / 1000.0;
    if (dt >= 1.0) {
        const fps = (frameCounter / dt).toFixed(1);
        const mbps = ((bytesCounter * 8) / (dt * 1000000)).toFixed(2);

        hudFps.innerText = fps;
        hudBitrate.innerText = `${mbps} Mbps`;

        frameCounter = 0;
        bytesCounter = 0;
        lastFpsTime = now;
    }
}, 1000);

// Initialize WASM Module and start networking
(async () => {
    try {
        loadingText.innerText = 'Initializing WebAssembly SIMD128 decoder...';
        phfMod = await getPHFModule();
        try {
            const cfg = await (await fetch('config.json', { cache: 'no-store' })).json();
            if (Number.isInteger(cfg.ws_port)) wsPort = cfg.ws_port;
        } catch (e) {
            console.warn('No server config.json, using default WebSocket port', wsPort);
        }
        connectWebSocket();
    } catch (err) {
        console.error('Failed to initialize PHF WASM Module:', err);
        loadingText.innerText = 'Error loading WebAssembly engine: ' + err.message;
    }
})();
