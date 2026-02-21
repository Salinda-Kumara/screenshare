"""
Web-based viewer — bridges the relay server to browsers via WebSocket.

Run this on any machine that can reach the relay server. Open the
printed URL in any modern browser to watch the screen share.

Usage:
    python web_viewer.py --host <relay-server-ip>
    python web_viewer.py                            # auto-discover on LAN
"""

import asyncio
import collections
import ipaddress
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time

import zstandard as zstd

try:
    from aiohttp import web
except ImportError:
    print("ERROR: aiohttp is required.  Install with:  pip install aiohttp")
    sys.exit(1)

from config import (
    SCREEN_PORT, AUDIO_PORT, CONTROL_PORT,
    AUDIO_RATE, AUDIO_CHANNELS, AUDIO_FORMAT_WIDTH,
    ROLE_VIEWER, ROLE_SHARER, MSG_SELECT_SHARER, MSG_SHARER_LIST,
    CONNECT_TIMEOUT, RECV_TIMEOUT, WEB_PORT,
    WEBRTC_ENABLED, WEBRTC_SIGNALING_PATH,
    WEBRTC_VIDEO_BITRATE, WEBRTC_MAX_BITRATE, WEBRTC_MIN_BITRATE,
    WEBRTC_PREFERRED_CODECS, WEBRTC_AUDIO_BITRATE, WEBRTC_ICE_SERVERS,
)
from network_utils import configure_socket, send_frame, recv_frame
from discovery import DiscoveryClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════════════════════
#  RELAY BRIDGE — TCP connection to relay server, buffers data
# ═══════════════════════════════════════════════════════════════

class RelayBridge:
    """Connects to the relay server as a viewer and makes data available
    for the web layer to push to browsers via WebSocket."""

    def __init__(self, host, screen_port=SCREEN_PORT,
                 audio_port=AUDIO_PORT, control_port=CONTROL_PORT):
        self.host = host
        self.screen_port = screen_port
        self.audio_port = audio_port
        self.control_port = control_port

        self.running = False

        self._dctx_screen = zstd.ZstdDecompressor()
        self._dctx_audio = zstd.ZstdDecompressor()

        # Screen (latest JPEG bytes)
        self._frame_lock = threading.Lock()
        self.latest_frame: bytes | None = None
        self.frame_seq = 0
        self._frame_event = threading.Event()  # signalled on new frame

        # Async viewer notification (set from thread, awaited from asyncio)
        self._viewer_events: set = set()
        self._viewer_events_lock = threading.Lock()
        self._event_loop = None

        # Audio (ring buffer of (seq, pcm_bytes))
        self._audio_lock = threading.Lock()
        self._audio_buffer: collections.deque = collections.deque(maxlen=500)
        self.audio_seq = 0

        # Control
        self._ctrl_lock = threading.Lock()
        self._ctrl_sock: socket.socket | None = None
        self.sharers: list = []
        self.active_sharer_id = None
        self.ctrl_version = 0

        # Connection flags (read from async code — safe via GIL)
        self.screen_connected = False
        self.audio_connected = False
        self.control_connected = False

    # ── lifecycle ──

    def start(self):
        self.running = True
        threading.Thread(target=self._screen_loop, daemon=True,
                         name="bridge-screen").start()
        threading.Thread(target=self._audio_loop, daemon=True,
                         name="bridge-audio").start()
        threading.Thread(target=self._control_loop, daemon=True,
                         name="bridge-control").start()
        log.info("Relay bridge started → %s", self.host)

    def stop(self):
        self.running = False

    # ── public helpers ──

    def select_sharer(self, sharer_id):
        """Forward a 'select sharer' command to the relay server."""
        with self._ctrl_lock:
            sock = self._ctrl_sock
        if sock:
            msg = json.dumps({
                "type": MSG_SELECT_SHARER,
                "sharer_id": sharer_id,
            }).encode("utf-8")
            try:
                send_frame(sock, msg)
            except Exception as exc:
                log.debug("select_sharer failed: %s", exc)

    def get_audio_since(self, since_seq):
        """Return list of (seq, pcm_bytes) newer than *since_seq*."""
        result = []
        with self._audio_lock:
            for seq, data in self._audio_buffer:
                if seq > since_seq:
                    result.append((seq, data))
        return result

    def bind_loop(self, loop):
        """Store the asyncio event loop for thread→async signalling."""
        self._event_loop = loop

    def register_viewer_event(self, evt):
        with self._viewer_events_lock:
            self._viewer_events.add(evt)

    def unregister_viewer_event(self, evt):
        with self._viewer_events_lock:
            self._viewer_events.discard(evt)

    def _signal_viewers(self):
        """Signal all waiting viewer WebSocket handlers (called from thread)."""
        loop = self._event_loop
        if not loop:
            return
        with self._viewer_events_lock:
            for evt in list(self._viewer_events):
                loop.call_soon_threadsafe(evt.set)

    # ── threads ──

    def _screen_loop(self):
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                configure_socket(sock)
                sock.settimeout(CONNECT_TIMEOUT)
                sock.connect((self.host, self.screen_port))
                sock.sendall(ROLE_VIEWER)
                sock.settimeout(RECV_TIMEOUT)
                self.screen_connected = True
                log.info("Screen relay connected → %s:%d",
                         self.host, self.screen_port)

                while self.running:
                    compressed = recv_frame(sock)
                    jpeg = self._dctx_screen.decompress(compressed)
                    with self._frame_lock:
                        self.latest_frame = jpeg
                        self.frame_seq += 1
                    self._frame_event.set()
                    self._signal_viewers()
                    self._frame_event.set()
                    self._signal_viewers()

            except Exception as exc:
                if self.running:
                    log.warning("Screen relay: %s", exc)
            finally:
                self.screen_connected = False
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if self.running:
                time.sleep(2)

    def _audio_loop(self):
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                configure_socket(sock)
                sock.settimeout(CONNECT_TIMEOUT)
                sock.connect((self.host, self.audio_port))
                sock.sendall(ROLE_VIEWER)
                sock.settimeout(RECV_TIMEOUT)
                self.audio_connected = True
                log.info("Audio relay connected → %s:%d",
                         self.host, self.audio_port)

                while self.running:
                    compressed = recv_frame(sock)
                    pcm = self._dctx_audio.decompress(compressed)
                    with self._audio_lock:
                        self.audio_seq += 1
                        self._audio_buffer.append((self.audio_seq, pcm))

            except Exception as exc:
                if self.running:
                    log.warning("Audio relay: %s", exc)
            finally:
                self.audio_connected = False
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if self.running:
                time.sleep(2)

    def _control_loop(self):
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                configure_socket(sock)
                sock.settimeout(CONNECT_TIMEOUT)
                sock.connect((self.host, self.control_port))
                sock.settimeout(120.0)  # Control channel is mostly idle
                with self._ctrl_lock:
                    self._ctrl_sock = sock
                self.control_connected = True
                log.info("Control relay connected → %s:%d",
                         self.host, self.control_port)

                while self.running:
                    data = recv_frame(sock)
                    msg = json.loads(data.decode("utf-8"))
                    if msg.get("type") == MSG_SHARER_LIST:
                        with self._ctrl_lock:
                            self.sharers = msg.get("sharers", [])
                            self.active_sharer_id = msg.get("active_sharer_id")
                            self.ctrl_version += 1

            except Exception as exc:
                if self.running:
                    log.warning("Control relay: %s", exc)
            finally:
                self.control_connected = False
                with self._ctrl_lock:
                    self._ctrl_sock = None
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if self.running:
                time.sleep(2)


# ═══════════════════════════════════════════════════════════════
#  SHARER BRIDGE — forwards browser screen/audio to relay server
# ═══════════════════════════════════════════════════════════════

class SharerBridge:
    """Connects to the relay server as a sharer and forwards
    screen frames and audio received from a browser WebSocket."""

    def __init__(self, host, sharer_name,
                 screen_port=SCREEN_PORT, audio_port=AUDIO_PORT):
        self.host = host
        self.sharer_name = sharer_name
        self.screen_port = screen_port
        self.audio_port = audio_port

        self._cctx = zstd.ZstdCompressor(level=1)

        self._screen_sock: socket.socket | None = None
        self._audio_sock: socket.socket | None = None
        self._screen_lock = threading.Lock()
        self._audio_lock = threading.Lock()

        self.screen_connected = False
        self.audio_connected = False
        self.sharer_id = None

    def connect_screen(self):
        """Connect to relay as a screen sharer (blocking)."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((self.host, self.screen_port))
            sock.settimeout(None)

            # Send role
            sock.sendall(ROLE_SHARER)

            # Send sharer info
            info = json.dumps({"name": self.sharer_name}).encode("utf-8")
            send_frame(sock, info)

            # Read back sharer_id assigned by relay
            try:
                resp_data = recv_frame(sock)
                resp = json.loads(resp_data.decode("utf-8"))
                self.sharer_id = resp.get("sharer_id")
                log.info("SharerBridge got sharer_id=%s", self.sharer_id)
            except Exception:
                log.warning("SharerBridge could not read sharer_id")

            with self._screen_lock:
                self._screen_sock = sock
            self.screen_connected = True
            log.info("SharerBridge screen connected → %s:%d (%s)",
                     self.host, self.screen_port, self.sharer_name)
            return True
        except Exception as exc:
            log.warning("SharerBridge screen connect failed: %s", exc)
            return False

    def connect_audio(self):
        """Connect to relay as an audio sharer (blocking)."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((self.host, self.audio_port))
            sock.settimeout(None)

            sock.sendall(ROLE_SHARER)

            info = json.dumps({
                "name": self.sharer_name,
                "sharer_id": self.sharer_id or -1,
            }).encode("utf-8")
            send_frame(sock, info)

            with self._audio_lock:
                self._audio_sock = sock
            self.audio_connected = True
            log.info("SharerBridge audio connected → %s:%d (%s)",
                     self.host, self.audio_port, self.sharer_name)
            return True
        except Exception as exc:
            log.warning("SharerBridge audio connect failed: %s", exc)
            return False

    def send_screen_frame(self, jpeg_bytes: bytes) -> bool:
        """Compress and send a JPEG frame to the relay."""
        with self._screen_lock:
            sock = self._screen_sock
        if not sock:
            return False
        try:
            compressed = self._cctx.compress(jpeg_bytes)
            return send_frame(sock, compressed)
        except Exception:
            self.screen_connected = False
            return False

    def send_audio_frame(self, pcm_bytes: bytes) -> bool:
        """Compress and send a PCM audio frame to the relay."""
        with self._audio_lock:
            sock = self._audio_sock
        if not sock:
            return False
        try:
            compressed = self._cctx.compress(pcm_bytes)
            return send_frame(sock, compressed)
        except Exception:
            self.audio_connected = False
            return False

    def close(self):
        """Disconnect from relay."""
        for lock, attr in [(self._screen_lock, "_screen_sock"),
                           (self._audio_lock, "_audio_sock")]:
            with lock:
                sock = getattr(self, attr)
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)
        self.screen_connected = False
        self.audio_connected = False
        log.info("SharerBridge closed (%s)", self.sharer_name)


# ═══════════════════════════════════════════════════════════════
#  WEB SERVER — aiohttp serves HTML + WebSocket endpoints
# ═══════════════════════════════════════════════════════════════

class WebViewerApp:
    """aiohttp application that serves the browser UI and bridges
    relay data over WebSocket to connected browsers."""

    def __init__(self, bridge: RelayBridge, web_port: int = WEB_PORT):
        self.bridge = bridge
        self.web_port = web_port
        self.app = web.Application()
        # WebRTC signaling rooms: {room_id: {"sharer": ws, "viewers": [ws, ...]}}
        self._rtc_rooms: dict = {}
        self._rtc_rooms_lock = asyncio.Lock()
        self._setup_routes()
        self.app.on_startup.append(self._on_startup)

    def _setup_routes(self):
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/ws/screen", self._handle_ws_screen)
        self.app.router.add_get("/ws/audio", self._handle_ws_audio)
        self.app.router.add_get("/ws/control", self._handle_ws_control)
        self.app.router.add_get("/ws/share/screen", self._handle_ws_share_screen)
        self.app.router.add_get("/ws/share/audio", self._handle_ws_share_audio)
        self.app.router.add_get(WEBRTC_SIGNALING_PATH, self._handle_ws_rtc_signaling)
        self.app.router.add_get("/api/status", self._handle_status)
        self.app.router.add_get("/api/rtc-config", self._handle_rtc_config)
        self.app.router.add_get("/api/debug", self._handle_debug)

        static_dir = os.path.join(BASE_DIR, "static")
        if os.path.isdir(static_dir):
            self.app.router.add_static("/static/", static_dir)

    async def _on_startup(self, app):
        self.bridge.bind_loop(asyncio.get_running_loop())

    # ── HTTP handlers ──

    async def _handle_index(self, request):
        path = os.path.join(BASE_DIR, "static", "index.html")
        if not os.path.isfile(path):
            return web.Response(text="static/index.html not found", status=404)
        # Disable caching so code updates reach the browser
        return web.FileResponse(path, headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        })

    async def _handle_status(self, request):
        return web.json_response({
            "relay_host": self.bridge.host,
            "screen_connected": self.bridge.screen_connected,
            "audio_connected": self.bridge.audio_connected,
            "control_connected": self.bridge.control_connected,
            "sharers": self.bridge.sharers,
            "active_sharer_id": self.bridge.active_sharer_id,
            "webrtc_enabled": WEBRTC_ENABLED,
        })

    async def _handle_rtc_config(self, request):
        """Return WebRTC configuration for browser peers."""
        return web.json_response({
            "enabled": WEBRTC_ENABLED,
            "iceServers": WEBRTC_ICE_SERVERS,
            "videoBitrate": WEBRTC_VIDEO_BITRATE,
            "maxBitrate": WEBRTC_MAX_BITRATE,
            "minBitrate": WEBRTC_MIN_BITRATE,
            "preferredCodecs": WEBRTC_PREFERRED_CODECS,
            "audioBitrate": WEBRTC_AUDIO_BITRATE,
        })

    async def _handle_debug(self, request):
        """Debug endpoint — shows full server state for troubleshooting."""
        return web.json_response({
            "relay_host": self.bridge.host,
            "screen_port": self.bridge.screen_port,
            "audio_port": self.bridge.audio_port,
            "control_port": self.bridge.control_port,
            "bridge": {
                "screen_connected": self.bridge.screen_connected,
                "audio_connected": self.bridge.audio_connected,
                "control_connected": self.bridge.control_connected,
                "frame_seq": self.bridge.frame_seq,
                "audio_seq": self.bridge.audio_seq,
                "sharers": self.bridge.sharers,
                "active_sharer_id": self.bridge.active_sharer_id,
            },
            "rtc_rooms": list(self._rtc_rooms.keys()) if self._rtc_rooms else [],
            "webrtc_enabled": WEBRTC_ENABLED,
        })

    # ── Screen WebSocket ──

    async def _handle_ws_screen(self, request):
        ws = web.WebSocketResponse(compress=False)
        await ws.prepare(request)
        log.info("Browser screen WS connected (%s)", request.remote)

        last_seq = 0
        my_event = asyncio.Event()
        self.bridge.register_viewer_event(my_event)

        try:
            while not ws.closed and self.bridge.running:
                frame = None
                with self.bridge._frame_lock:
                    if self.bridge.frame_seq > last_seq:
                        frame = self.bridge.latest_frame
                        last_seq = self.bridge.frame_seq

                if frame:
                    await ws.send_bytes(frame)
                else:
                    my_event.clear()
                    with self.bridge._frame_lock:
                        if self.bridge.frame_seq > last_seq:
                            continue
                    try:
                        await asyncio.wait_for(my_event.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
                        pass
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("Screen WS: %s", exc)
        finally:
            self.bridge.unregister_viewer_event(my_event)
            log.info("Browser screen WS disconnected")
        return ws

    # ── Audio WebSocket ──

    async def _handle_ws_audio(self, request):
        ws = web.WebSocketResponse(compress=False)
        await ws.prepare(request)
        log.info("Browser audio WS connected (%s)", request.remote)

        # Start from NOW — don't replay old audio
        last_seq = self.bridge.audio_seq

        try:
            while not ws.closed and self.bridge.running:
                chunks = self.bridge.get_audio_since(last_seq)
                for seq, pcm in chunks:
                    await ws.send_bytes(pcm)
                    last_seq = seq
                if not chunks:
                    await asyncio.sleep(0.005)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("Audio WS: %s", exc)
        finally:
            log.info("Browser audio WS disconnected")
        return ws

    # ── Control WebSocket ──

    async def _handle_ws_control(self, request):
        ws = web.WebSocketResponse(compress=False)
        await ws.prepare(request)
        log.info("Browser control WS connected (%s)", request.remote)

        async def push_updates():
            last_state = None
            try:
                while not ws.closed and self.bridge.running:
                    with self.bridge._ctrl_lock:
                        state = json.dumps({
                            "type": "state",
                            "sharers": list(self.bridge.sharers),
                            "active_sharer_id": self.bridge.active_sharer_id,
                            "screen_connected": self.bridge.screen_connected,
                            "audio_connected": self.bridge.audio_connected,
                            "control_connected": self.bridge.control_connected,
                        })
                    if state != last_state:
                        last_state = state
                        await ws.send_str(state)
                    await asyncio.sleep(0.5)
            except (ConnectionResetError, asyncio.CancelledError):
                pass

        async def recv_commands():
            try:
                async for msg in ws:
                    if msg.type == web.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            if data.get("type") == "select_sharer":
                                self.bridge.select_sharer(data["sharer_id"])
                        except Exception:
                            pass
                    elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                        break
            except (ConnectionResetError, asyncio.CancelledError):
                pass

        push_task = asyncio.create_task(push_updates())
        recv_task = asyncio.create_task(recv_commands())

        try:
            done, pending = await asyncio.wait(
                [push_task, recv_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        except Exception:
            push_task.cancel()
            recv_task.cancel()

        log.info("Browser control WS disconnected")
        return ws

    # ── Sharer Screen WebSocket ──

    async def _handle_ws_share_screen(self, request):
        ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024, compress=False)  # 10MB
        await ws.prepare(request)

        name = request.query.get("name", f"Browser ({request.remote})")
        log.info("Browser sharer screen WS connected: %s (%s)", name, request.remote)

        sharer = SharerBridge(
            self.bridge.host, name,
            screen_port=self.bridge.screen_port,
            audio_port=self.bridge.audio_port,
        )

        # Connect to relay in a thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        log.info("SharerBridge connecting to relay %s:%d ...",
                 self.bridge.host, self.bridge.screen_port)
        ok = await loop.run_in_executor(None, sharer.connect_screen)
        if not ok:
            log.error("SharerBridge FAILED to connect to relay %s:%d",
                      self.bridge.host, self.bridge.screen_port)
            await ws.close(code=1011, message=b"Cannot connect to relay server")
            return ws
        log.info("SharerBridge connected OK, sharer_id=%s", sharer.sharer_id)

        # Store sharer bridge so audio WS can find it
        sid = sharer.sharer_id
        if sid is not None:
            if not hasattr(self, '_active_sharers'):
                self._active_sharers = {}
            self._active_sharers[sid] = sharer

        # Send sharer_id back so the audio WS can reference it
        await ws.send_str(json.dumps({
            "type": "connected",
            "sharer_id": sharer.sharer_id,
        }))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    ok = await loop.run_in_executor(
                        None, sharer.send_screen_frame, msg.data)
                    if not ok:
                        break
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("Sharer screen WS: %s", exc)
        finally:
            if sid is not None and hasattr(self, '_active_sharers'):
                self._active_sharers.pop(sid, None)
            await loop.run_in_executor(None, sharer.close)
            log.info("Browser sharer screen WS disconnected: %s", name)
        return ws

    # ── Sharer Audio WebSocket ──

    async def _handle_ws_share_audio(self, request):
        ws = web.WebSocketResponse(max_msg_size=1 * 1024 * 1024, compress=False)  # 1MB
        await ws.prepare(request)

        name = request.query.get("name", f"Browser ({request.remote})")
        sharer_id_str = request.query.get("sharer_id", "")
        log.info("Browser sharer audio WS connected: %s (%s) sharer_id=%s",
                 name, request.remote, sharer_id_str)

        # Create a new SharerBridge for audio only, but set the correct sharer_id
        sharer = SharerBridge(
            self.bridge.host, name,
            screen_port=self.bridge.screen_port,
            audio_port=self.bridge.audio_port,
        )

        # Set sharer_id from query param so relay can link audio to screen
        try:
            sharer.sharer_id = int(sharer_id_str)
        except (ValueError, TypeError):
            log.warning("Invalid sharer_id for audio WS: %s", sharer_id_str)

        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, sharer.connect_audio)
        if not ok:
            await ws.close(code=1011, message=b"Cannot connect to relay server")
            return ws

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    ok = await loop.run_in_executor(
                        None, sharer.send_audio_frame, msg.data)
                    if not ok:
                        break
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("Sharer audio WS: %s", exc)
        finally:
            await loop.run_in_executor(None, sharer.close)
            log.info("Browser sharer audio WS disconnected: %s", name)
        return ws

    # ── WebRTC Signaling WebSocket ──
    #
    # Pure signaling relay: the server never touches media.
    # Sharer creates a room, viewers join.  SDP offers/answers and
    # ICE candidates are forwarded between peers so they can establish
    # a direct UDP (DTLS-SRTP) media channel on the LAN.

    async def _handle_ws_rtc_signaling(self, request):
        ws = web.WebSocketResponse(compress=False)
        await ws.prepare(request)

        role = request.query.get("role", "viewer")   # "sharer" | "viewer"
        room = request.query.get("room", "default")   # room id (= sharer name)
        peer_id = id(ws)

        log.info("WebRTC signaling WS: role=%s room=%s peer=%d (%s)",
                 role, room, peer_id, request.remote)

        async with self._rtc_rooms_lock:
            if room not in self._rtc_rooms:
                self._rtc_rooms[room] = {"sharer": None, "viewers": {}}

            room_data = self._rtc_rooms[room]
            if role == "sharer":
                # Kick old sharer if any
                old = room_data["sharer"]
                if old and not old.closed:
                    await old.send_str(json.dumps({"type": "kicked"}))
                    await old.close()
                room_data["sharer"] = ws
            else:
                room_data["viewers"][peer_id] = ws

        # Notify sharer of new viewer joining
        if role == "viewer":
            async with self._rtc_rooms_lock:
                sharer_ws = self._rtc_rooms.get(room, {}).get("sharer")
            if sharer_ws and not sharer_ws.closed:
                try:
                    await sharer_ws.send_str(json.dumps({
                        "type": "viewer_joined",
                        "peerId": peer_id,
                    }))
                except Exception:
                    pass

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue

                    data["from"] = peer_id
                    msg_type = data.get("type", "")

                    async with self._rtc_rooms_lock:
                        rd = self._rtc_rooms.get(room)
                        if not rd:
                            continue

                    if role == "sharer":
                        # Sharer → specific viewer (offer, answer, candidate)
                        target = data.get("to")
                        if target:
                            async with self._rtc_rooms_lock:
                                vws = rd["viewers"].get(target)
                            if vws and not vws.closed:
                                await vws.send_str(json.dumps(data))
                        else:
                            # Broadcast to all viewers
                            async with self._rtc_rooms_lock:
                                viewers = list(rd["viewers"].values())
                            for v in viewers:
                                if not v.closed:
                                    try:
                                        await v.send_str(json.dumps(data))
                                    except Exception:
                                        pass
                    else:
                        # Viewer → sharer (answer, candidate)
                        async with self._rtc_rooms_lock:
                            sws = rd["sharer"]
                        if sws and not sws.closed:
                            await sws.send_str(json.dumps(data))

                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("RTC signaling WS error: %s", exc)
        finally:
            # Cleanup
            async with self._rtc_rooms_lock:
                rd = self._rtc_rooms.get(room)
                if rd:
                    if role == "sharer" and rd["sharer"] is ws:
                        rd["sharer"] = None
                        # Notify viewers that sharer left
                        for v in rd["viewers"].values():
                            if not v.closed:
                                try:
                                    await v.send_str(json.dumps({"type": "sharer_left"}))
                                except Exception:
                                    pass
                    elif role == "viewer":
                        rd["viewers"].pop(peer_id, None)
                        # Notify sharer that viewer left
                        sws = rd["sharer"]
                        if sws and not sws.closed:
                            try:
                                await sws.send_str(json.dumps({
                                    "type": "viewer_left",
                                    "peerId": peer_id,
                                }))
                            except Exception:
                                pass
                    # Remove empty rooms
                    if not rd["sharer"] and not rd["viewers"]:
                        del self._rtc_rooms[room]

            log.info("WebRTC signaling WS disconnected: role=%s room=%s peer=%d",
                     role, room, peer_id)
        return ws

    # ── run ──

    def run(self, ssl_context=None):
        web.run_app(self.app, host="0.0.0.0", port=self.web_port,
                    ssl_context=ssl_context)


# ═══════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _generate_self_signed_cert(cert_path, key_path):
    """Generate a self-signed certificate for HTTPS."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "LAN Screen Share"),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.DNSName("*"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv4Address("0.0.0.0")),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        log.info("Generated self-signed certificate: %s", cert_path)
        return True
    except ImportError:
        log.warning("'cryptography' package not installed — cannot auto-generate SSL cert.")
        log.warning("Install with:  pip install cryptography")
        log.warning("Falling back to HTTP (browser sharing will NOT work on remote clients).")
        return False
    except Exception as exc:
        log.warning("Failed to generate self-signed cert: %s", exc)
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="LAN Screen Share — Web Viewer")
    parser.add_argument("--host",
                        help="Relay server IP (auto-discovers if omitted)")
    parser.add_argument("--web-port", type=int, default=WEB_PORT,
                        help="Web server port (default: %(default)s)")
    parser.add_argument("--screen-port", type=int, default=SCREEN_PORT)
    parser.add_argument("--audio-port", type=int, default=AUDIO_PORT)
    parser.add_argument("--control-port", type=int, default=CONTROL_PORT)
    parser.add_argument("--ssl-cert",
                        help="Path to SSL certificate (PEM). Auto-generated if omitted.")
    parser.add_argument("--ssl-key",
                        help="Path to SSL private key (PEM). Auto-generated if omitted.")
    parser.add_argument("--no-ssl", action="store_true",
                        help="Disable HTTPS (browser sharing won't work remotely)")
    args = parser.parse_args()

    relay_host = args.host
    if not relay_host:
        print("Searching for relay server on LAN...")
        servers = DiscoveryClient.discover(timeout=3)
        if servers:
            srv = servers[0]
            relay_host = srv["ip"]
            print(f"Found: {srv.get('name', relay_host)} ({relay_host})")
        else:
            try:
                relay_host = input(
                    "No server found. Enter relay server IP: ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not relay_host:
                print("No host specified. Exiting.")
                return

    # ── SSL setup ──
    ssl_ctx = None
    scheme = "http"
    if not args.no_ssl:
        cert_path = args.ssl_cert
        key_path = args.ssl_key
        if cert_path and key_path:
            # User-provided cert
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(cert_path, key_path)
            scheme = "https"
        else:
            # Auto-generate self-signed cert
            auto_cert = os.path.join(BASE_DIR, "cert.pem")
            auto_key = os.path.join(BASE_DIR, "key.pem")
            need_gen = not (os.path.exists(auto_cert) and os.path.exists(auto_key))
            if need_gen:
                ok = _generate_self_signed_cert(auto_cert, auto_key)
            else:
                ok = True
            if ok:
                ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ssl_ctx.load_cert_chain(auto_cert, auto_key)
                scheme = "https"

    local_ip = get_local_ip()
    print(f"\n{'=' * 55}")
    print(f"  LAN Screen Share — Web Viewer")
    print(f"{'=' * 55}")
    print(f"  Relay server : {relay_host}")
    print(f"  Web UI       : {scheme}://{local_ip}:{args.web_port}")
    print(f"  WebRTC       : {'enabled (AV1/H264/VP9 peer-to-peer)' if WEBRTC_ENABLED else 'disabled'}")
    if scheme == "https":
        print(f"  SSL          : enabled (self-signed)")
        print(f"  NOTE: Accept the browser certificate warning on first visit.")
    else:
        print(f"  SSL          : disabled")
        print(f"  WARNING: Screen sharing from browser requires HTTPS.")
    print(f"{'=' * 55}")
    print(f"\n  Open the URL above in any browser to view.\n")

    bridge = RelayBridge(
        relay_host,
        screen_port=args.screen_port,
        audio_port=args.audio_port,
        control_port=args.control_port,
    )
    bridge.start()

    server = WebViewerApp(bridge, web_port=args.web_port)
    try:
        server.run(ssl_context=ssl_ctx)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()
