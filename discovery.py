"""LAN auto-discovery using UDP broadcast."""

import socket
import threading
import time
import json
import logging

from config import (
    DISCOVERY_PORT, BROADCAST_ADDR,
    DISCOVERY_MSG, DISCOVERY_RESPONSE_PREFIX
)

log = logging.getLogger(__name__)


class DiscoveryBroadcaster:
    """Broadcasts presence on LAN so viewers can find this broadcaster."""

    def __init__(self, screen_port, audio_port, name="Screen Share"):
        self.screen_port = screen_port
        self.audio_port = audio_port
        self.name = name
        self.running = False
        self._thread = None
        self._sock = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        log.info("Discovery broadcaster started on port %d", DISCOVERY_PORT)

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        log.info("Discovery broadcaster stopped")

    def _listen(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        try:
            self._sock.bind(("", DISCOVERY_PORT))
        except OSError as e:
            log.error("Cannot bind discovery port %d: %s", DISCOVERY_PORT, e)
            return

        while self.running:
            try:
                data, addr = self._sock.recvfrom(1024)
                if data == DISCOVERY_MSG:
                    response_data = json.dumps({
                        "name": self.name,
                        "screen_port": self.screen_port,
                        "audio_port": self.audio_port
                    }).encode("utf-8")
                    response = DISCOVERY_RESPONSE_PREFIX + response_data
                    self._sock.sendto(response, addr)
                    log.info("Responded to discovery from %s", addr[0])
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    continue
                break
        try:
            self._sock.close()
        except Exception:
            pass


class DiscoveryClient:
    """Discovers broadcasters on LAN."""

    @staticmethod
    def discover(timeout=3):
        """Send discovery broadcast and collect responses."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)

        broadcasters = []
        seen_ips = set()

        try:
            sock.sendto(DISCOVERY_MSG, (BROADCAST_ADDR, DISCOVERY_PORT))
            start = time.time()

            while time.time() - start < timeout:
                try:
                    data, addr = sock.recvfrom(4096)
                    if data.startswith(DISCOVERY_RESPONSE_PREFIX) and addr[0] not in seen_ips:
                        json_data = data[len(DISCOVERY_RESPONSE_PREFIX):]
                        info = json.loads(json_data.decode("utf-8"))
                        info["ip"] = addr[0]
                        broadcasters.append(info)
                        seen_ips.add(addr[0])
                except socket.timeout:
                    continue
                except Exception:
                    continue
        except Exception as e:
            log.error("Discovery failed: %s", e)
        finally:
            sock.close()

        return broadcasters
