#!/bin/bash
# ─────────────────────────────────────────────────────
#  LAN Screen Share — Relay Server + Web Viewer
# ─────────────────────────────────────────────────────

set -e

echo "============================================"
echo "  LAN Screen Share — Server + Web Viewer"
echo "============================================"
echo ""
echo "  Discovery : 9876/udp"
echo "  Screen    : 9877/tcp"
echo "  Audio     : 9878/tcp"
echo "  Control   : 9879/tcp"
echo "  Web UI    : 1000/tcp  (open in browser)"
echo ""
echo "============================================"

# Start the web viewer in the background (connects to localhost relay)
python web_viewer.py --host 127.0.0.1 --web-port 1000 &
WEB_PID=$!

# Trap signals so both processes shut down together
cleanup() {
    echo "Shutting down..."
    kill $WEB_PID 2>/dev/null || true
    wait $WEB_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# Start the relay server in the foreground
python relay_server.py &
RELAY_PID=$!

# Wait for either to exit
wait -n $RELAY_PID $WEB_PID 2>/dev/null || true
cleanup
