#!/usr/bin/env python3
"""
PHF Live Stream Server
Ultra-low latency, 100% lossless webcam and audio streaming over WebSocket with WebAssembly client.
"""

import os
import sys
import argparse
import asyncio

# Ensure safe output on Windows console
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add server directory to path
server_dir = os.path.join(os.path.dirname(__file__), "server")
sys.path.insert(0, server_dir)

from stream_server import StreamServer

def main():
    parser = argparse.ArgumentParser(description="PHF Live Stream Server (Webcam & Audio)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to listen on (default: 127.0.0.1, this machine only). "
                             "Use 0.0.0.0 to expose camera and microphone to the whole network.")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP dashboard port (default: 8080)")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket streaming port (default: 8765)")
    args = parser.parse_args()

    web_dir = os.path.join(os.path.dirname(__file__), "web")
    server = StreamServer(web_dir=web_dir, host=args.host, http_port=args.http_port, ws_port=args.ws_port)

    print("=" * 60)
    print("  🚀 PHF Live Stream Server")
    print("=" * 60)
    print(f"  👉 Web Dashboard:    http://localhost:{args.http_port}")
    print(f"  👉 WebSocket Stream:  ws://localhost:{args.ws_port}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  ⚠️  Listening on {args.host}: anyone who can reach this machine can watch")
        print("      the camera and turn on the microphone. There is no authentication.")
    print("=" * 60)
    print("Press Ctrl+C to terminate.")

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\n[Server] Shutting down...")
    finally:
        server.stop()

if __name__ == "__main__":
    main()
