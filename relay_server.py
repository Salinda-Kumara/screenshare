"""
Relay Server — runs on Docker (Ubuntu).

Architecture:
  - Multiple SHARERS connect and send their screen+audio.
  - Multiple VIEWERS (including the projection PC) connect and receive.
  - Server relays the ACTIVE sharer's stream to all viewers.
  - A control channel lets clients see who's sharing and switch.

Ports:
  9876/udp  - LAN discovery
  9877/tcp  - Screen relay
  9878/tcp  - Audio relay
  9879/tcp  - Control / signaling
"""

import socket
import threading
import json
import time
import logging
import struct

from config import (
    SCREEN_PORT, AUDIO_PORT, CONTROL_PORT, DISCOVERY_PORT,
    ROLE_VIEWER, ROLE_SHARER,
    MSG_SHARER_LIST, MSG_SERVER_INFO, MSG_KICK_SHARER,
    MSG_START_SHARE, MSG_STOP_SHARE, MSG_SELECT_SHARER,
    HEADER_SIZE, HEADER_FORMAT, MAGIC, SOCKET_BUFFER,
    BROADCAST_ADDR, DISCOVERY_MSG, DISCOVERY_RESPONSE_PREFIX,
    WEBRTC_ENABLED,
)
from network_utils import configure_socket, send_frame, recv_frame

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("relay_server")


class RelayServer:
    """Central relay server for LAN screen sharing meetings."""

    def __init__(self):
        # Sharers: {sharer_id: {"name":str, "screen_sock":sock, "audio_sock":sock, "ip":str}}
        self.sharers = {}
        self.sharers_lock = threading.Lock()
        self.sharer_counter = 0

        # Viewers: screen and audio sockets
        self.screen_viewers = []
        self.screen_viewers_lock = threading.Lock()
        self.audio_viewers = []
        self.audio_viewers_lock = threading.Lock()

        # Control clients
        self.control_clients = []
        self.control_lock = threading.Lock()

        # Active sharer (whose stream is being relayed)
        self.active_sharer_id = None
        self.active_lock = threading.Lock()

        self.running = False

    def start(self):
        self.running = True

        threads = [
            threading.Thread(target=self._run_screen_relay, daemon=True),
            threading.Thread(target=self._run_audio_relay, daemon=True),
            threading.Thread(target=self._run_control_server, daemon=True),
            threading.Thread(target=self._run_discovery, daemon=True),
        ]
        for t in threads:
            t.start()

        log.info("=" * 55)
        log.info("  LAN Screen Share — Relay Server Started")
        log.info("=" * 55)
        log.info("  Screen relay : port %d", SCREEN_PORT)
        log.info("  Audio relay  : port %d", AUDIO_PORT)
        log.info("  Control      : port %d", CONTROL_PORT)
        log.info("  Discovery    : port %d/udp", DISCOVERY_PORT)
        log.info("  WebRTC       : %s (browser peer-to-peer)",
                 "enabled" if WEBRTC_ENABLED else "disabled")
        log.info("=" * 55)

        try:
            while self.running:
                time.sleep(5)
                self._log_status()
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.running = False

    def _log_status(self):
        with self.sharers_lock:
            n_sharers = len(self.sharers)
            sharer_names = [s["name"] for s in self.sharers.values()]
        with self.screen_viewers_lock:
            n_viewers = len(self.screen_viewers)
        active = self.active_sharer_id
        log.info("Sharers: %d %s | Viewers: %d | Active: %s",
                 n_sharers, sharer_names, n_viewers, active)

    # ───────── DISCOVERY ─────────

    def _run_discovery(self):
        """Respond to UDP discovery broadcasts."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        sock.bind(("", DISCOVERY_PORT))
        log.info("Discovery listener on port %d/udp", DISCOVERY_PORT)

        hostname = socket.gethostname()
        while self.running:
            try:
                data, addr = sock.recvfrom(1024)
                if data == DISCOVERY_MSG:
                    resp = json.dumps({
                        "name": f"Meeting Server ({hostname})",
                        "screen_port": SCREEN_PORT,
                        "audio_port": AUDIO_PORT,
                        "control_port": CONTROL_PORT,
                        "type": "relay_server",
                        "webrtc_enabled": WEBRTC_ENABLED,
                    }).encode("utf-8")
                    sock.sendto(DISCOVERY_RESPONSE_PREFIX + resp, addr)
                    log.info("Discovery reply → %s", addr[0])
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    continue
        sock.close()

    # ───────── SCREEN RELAY ─────────

    def _run_screen_relay(self):
        """Accept screen connections, route by role."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        configure_socket(srv, is_server=True)
        srv.settimeout(1.0)
        srv.bind(("0.0.0.0", SCREEN_PORT))
        srv.listen(20)
        log.info("Screen relay listening on port %d", SCREEN_PORT)

        while self.running:
            try:
                client, addr = srv.accept()
                configure_socket(client)
                client.settimeout(10.0)
                # First byte = role
                role = client.recv(1)
                if role == ROLE_SHARER:
                    threading.Thread(target=self._handle_screen_sharer,
                                     args=(client, addr), daemon=True).start()
                elif role == ROLE_VIEWER:
                    client.settimeout(5.0)  # Drop slow viewers instead of blocking everyone
                    with self.screen_viewers_lock:
                        self.screen_viewers.append(client)
                    log.info("Screen viewer connected: %s", addr[0])
                else:
                    client.close()
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    continue
        srv.close()

    def _handle_screen_sharer(self, sock, addr):
        """Receive screen frames from a sharer, relay to viewers if active."""
        # Read sharer name (first frame = JSON with name)
        try:
            info_data = recv_frame(sock)
            info = json.loads(info_data.decode("utf-8"))
            name = info.get("name", addr[0])
        except Exception:
            name = addr[0]

        sharer_id = self._register_sharer(name, addr[0], screen_sock=sock)
        log.info("Screen sharer connected: %s (id=%d) from %s", name, sharer_id, addr[0])
        self._broadcast_sharer_list()

        # Send sharer_id back so client can link audio stream
        try:
            resp = json.dumps({"sharer_id": sharer_id}).encode("utf-8")
            send_frame(sock, resp)
        except Exception:
            log.warning("Failed to send sharer_id to %s", name)

        # Remove the tight initial timeout — browser sharers need time to
        # start getDisplayMedia → encode first frame.  Use a generous
        # timeout so we still detect dead connections.
        sock.settimeout(60.0)

        try:
            while self.running:
                frame = recv_frame(sock)

                # Only relay if this is the active sharer
                with self.active_lock:
                    if self.active_sharer_id != sharer_id:
                        continue

                # Relay to all viewers (copy list so lock is held briefly)
                with self.screen_viewers_lock:
                    viewers_snapshot = list(self.screen_viewers)

                dead = []
                for v in viewers_snapshot:
                    if not send_frame(v, frame):
                        dead.append(v)

                if dead:
                    with self.screen_viewers_lock:
                        for d in dead:
                            try:
                                d.close()
                            except Exception:
                                pass
                            if d in self.screen_viewers:
                                self.screen_viewers.remove(d)

        except ConnectionError:
            pass
        except Exception as e:
            log.debug("Screen sharer %s error: %s", name, e)
        finally:
            self._unregister_sharer(sharer_id)
            try:
                sock.close()
            except Exception:
                pass
            log.info("Screen sharer disconnected: %s (id=%d)", name, sharer_id)
            self._broadcast_sharer_list()

    # ───────── AUDIO RELAY ─────────

    def _run_audio_relay(self):
        """Accept audio connections, route by role."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        configure_socket(srv, is_server=True)
        srv.settimeout(1.0)
        srv.bind(("0.0.0.0", AUDIO_PORT))
        srv.listen(20)
        log.info("Audio relay listening on port %d", AUDIO_PORT)

        while self.running:
            try:
                client, addr = srv.accept()
                configure_socket(client)
                client.settimeout(10.0)
                role = client.recv(1)
                if role == ROLE_SHARER:
                    threading.Thread(target=self._handle_audio_sharer,
                                     args=(client, addr), daemon=True).start()
                elif role == ROLE_VIEWER:
                    client.settimeout(5.0)  # Drop slow viewers instead of blocking everyone
                    with self.audio_viewers_lock:
                        self.audio_viewers.append(client)
                    log.info("Audio viewer connected: %s", addr[0])
                else:
                    client.close()
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    continue
        srv.close()

    def _handle_audio_sharer(self, sock, addr):
        """Receive audio from sharer, relay if active."""
        # Read sharer info
        try:
            info_data = recv_frame(sock)
            info = json.loads(info_data.decode("utf-8"))
            sharer_id = info.get("sharer_id", -1)
            name = info.get("name", addr[0])
        except Exception:
            sharer_id = -1
            name = addr[0]

        # Link audio socket to existing sharer record
        with self.sharers_lock:
            if sharer_id in self.sharers:
                self.sharers[sharer_id]["audio_sock"] = sock
        log.info("Audio sharer connected: %s (id=%d)", name, sharer_id)

        # Generous timeout for browser sharers
        sock.settimeout(60.0)

        try:
            while self.running:
                frame = recv_frame(sock)

                with self.active_lock:
                    if self.active_sharer_id != sharer_id:
                        continue

                with self.audio_viewers_lock:
                    audio_snapshot = list(self.audio_viewers)

                dead = []
                for v in audio_snapshot:
                    if not send_frame(v, frame):
                        dead.append(v)

                if dead:
                    with self.audio_viewers_lock:
                        for d in dead:
                            try:
                                d.close()
                            except Exception:
                                pass
                            if d in self.audio_viewers:
                                self.audio_viewers.remove(d)

        except ConnectionError:
            pass
        except Exception as e:
            log.debug("Audio sharer %s error: %s", name, e)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # ───────── CONTROL CHANNEL ─────────

    def _run_control_server(self):
        """Control channel for signaling (who's sharing, switch, etc.)."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        configure_socket(srv, is_server=True)
        srv.settimeout(1.0)
        srv.bind(("0.0.0.0", CONTROL_PORT))
        srv.listen(20)
        log.info("Control server on port %d", CONTROL_PORT)

        while self.running:
            try:
                client, addr = srv.accept()
                configure_socket(client)
                client.settimeout(30.0)
                with self.control_lock:
                    self.control_clients.append(client)
                threading.Thread(target=self._handle_control_client,
                                 args=(client, addr), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    continue
        srv.close()

    def _handle_control_client(self, sock, addr):
        """Handle control messages from a client."""
        log.info("Control client connected: %s", addr[0])
        # Use generous timeout — control channel is mostly idle
        sock.settimeout(120.0)

        # Send current sharer list immediately
        self._send_sharer_list(sock)

        try:
            while self.running:
                data = recv_frame(sock)
                msg = json.loads(data.decode("utf-8"))
                msg_type = msg.get("type")

                if msg_type == MSG_SELECT_SHARER:
                    sharer_id = msg.get("sharer_id")
                    self._set_active_sharer(sharer_id)

                elif msg_type == MSG_SHARER_LIST:
                    self._send_sharer_list(sock)

        except ConnectionError:
            pass
        except Exception as e:
            log.debug("Control client %s error: %s", addr[0], e)
        finally:
            with self.control_lock:
                if sock in self.control_clients:
                    self.control_clients.remove(sock)
            try:
                sock.close()
            except Exception:
                pass
            log.info("Control client disconnected: %s", addr[0])

    def _send_sharer_list(self, sock):
        """Send current sharer list to one client."""
        with self.sharers_lock:
            sharers = [
                {"id": sid, "name": s["name"], "ip": s["ip"]}
                for sid, s in self.sharers.items()
            ]
        with self.active_lock:
            active = self.active_sharer_id

        msg = json.dumps({
            "type": MSG_SHARER_LIST,
            "sharers": sharers,
            "active_sharer_id": active,
        }).encode("utf-8")
        send_frame(sock, msg)

    def _broadcast_sharer_list(self):
        """Send updated sharer list to ALL control clients."""
        with self.sharers_lock:
            sharers = [
                {"id": sid, "name": s["name"], "ip": s["ip"]}
                for sid, s in self.sharers.items()
            ]
        with self.active_lock:
            active = self.active_sharer_id

        msg = json.dumps({
            "type": MSG_SHARER_LIST,
            "sharers": sharers,
            "active_sharer_id": active,
        }).encode("utf-8")

        dead = []
        with self.control_lock:
            for c in self.control_clients:
                if not send_frame(c, msg):
                    dead.append(c)
            for d in dead:
                try:
                    d.close()
                except Exception:
                    pass
                self.control_clients.remove(d)

    # ───────── SHARER MANAGEMENT ─────────

    def _register_sharer(self, name, ip, screen_sock=None):
        with self.sharers_lock:
            self.sharer_counter += 1
            sid = self.sharer_counter
            self.sharers[sid] = {
                "name": name,
                "ip": ip,
                "screen_sock": screen_sock,
                "audio_sock": None,
            }

        # Auto-activate if first sharer
        with self.active_lock:
            if self.active_sharer_id is None:
                self.active_sharer_id = sid
                log.info("Auto-activated sharer: %s (id=%d)", name, sid)

        return sid

    def _unregister_sharer(self, sharer_id):
        with self.sharers_lock:
            self.sharers.pop(sharer_id, None)

        with self.active_lock:
            if self.active_sharer_id == sharer_id:
                # Switch to next available sharer
                with self.sharers_lock:
                    if self.sharers:
                        self.active_sharer_id = next(iter(self.sharers))
                        log.info("Switched to sharer id=%d", self.active_sharer_id)
                    else:
                        self.active_sharer_id = None
                        log.info("No active sharers")

    def _set_active_sharer(self, sharer_id):
        with self.sharers_lock:
            if sharer_id not in self.sharers:
                log.warning("Sharer id=%s not found", sharer_id)
                return

        with self.active_lock:
            self.active_sharer_id = sharer_id

        with self.sharers_lock:
            name = self.sharers[sharer_id]["name"]
        log.info("Active sharer set to: %s (id=%d)", name, sharer_id)
        self._broadcast_sharer_list()


def main():
    server = RelayServer()
    server.start()


if __name__ == "__main__":
    main()
