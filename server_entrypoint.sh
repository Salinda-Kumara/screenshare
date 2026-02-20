#!/bin/bash
# ─────────────────────────────────────────────────────
#  LAN Screen Share — Relay Server Entrypoint
# ─────────────────────────────────────────────────────

set -e

echo "============================================"
echo "  LAN Screen Share — Relay Server"
echo "============================================"
echo ""
echo "  Discovery : 9876/udp"
echo "  Screen    : 9877/tcp"
echo "  Audio     : 9878/tcp"
echo "  Control   : 9879/tcp"
echo ""
echo "============================================"

exec python relay_server.py
