"""Screen capture and display handling using mss (fast & reliable)."""

import threading
import time
import socket
import logging

import cv2
import numpy as np
import mss
import zstandard as zstd

from config import (
    SCREEN_PORT, SCREEN_FPS, SCREEN_QUALITY,
    SCREEN_RESIZE_FACTOR, CONNECT_TIMEOUT, RECV_TIMEOUT
)
from network_utils import configure_socket, send_frame, recv_frame

log = logging.getLogger(__name__)


class ScreenCapture:
    """Captures the screen and streams to connected viewers."""

    def __init__(self, port=SCREEN_PORT, fps=SCREEN_FPS, quality=SCREEN_QUALITY,
                 resize_factor=SCREEN_RESIZE_FACTOR):
        self.port = port
        self.fps = fps
        self.quality = quality
        self.resize_factor = resize_factor
        self.running = False
        self.clients = []
        self.clients_lock = threading.Lock()
        self.compressor = zstd.ZstdCompressor(level=1)
        self._server_sock = None

    def start(self):
        self.running = True
        self._accept_thread = threading.Thread(target=self._accept_connections, daemon=True)
        self._accept_thread.start()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        log.info("Screen capture started on port %d (FPS=%d, Q=%d)", self.port, self.fps, self.quality)

    def stop(self):
        self.running = False
        with self.clients_lock:
            for client in self.clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass
            self.clients.clear()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        log.info("Screen capture stopped")

    def _accept_connections(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        configure_socket(self._server_sock, is_server=True)
        self._server_sock.settimeout(1.0)
        try:
            self._server_sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            log.error("Cannot bind screen port %d: %s", self.port, e)
            self.running = False
            return
        self._server_sock.listen(10)

        while self.running:
            try:
                client, addr = self._server_sock.accept()
                configure_socket(client)
                with self.clients_lock:
                    self.clients.append(client)
                log.info("Screen viewer connected from %s", addr[0])
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    continue
                break

    def _capture_loop(self):
        interval = 1.0 / self.fps

        with mss.mss() as sct:
            monitor = sct.monitors[0]  # Full screen (all monitors combined)

            while self.running:
                loop_start = time.perf_counter()

                try:
                    # Capture with mss (much faster than ImageGrab)
                    screenshot = sct.grab(monitor)
                    frame = np.array(screenshot)

                    # mss gives BGRA, convert to BGR
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                    # Resize if needed
                    if self.resize_factor != 1.0:
                        h, w = frame.shape[:2]
                        new_w = int(w * self.resize_factor)
                        new_h = int(h * self.resize_factor)
                        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

                    # Scale to 1920x1080 if larger (aspect-ratio aware)
                    h, w = frame.shape[:2]
                    target_w, target_h = 1920, 1080
                    if w > target_w or h > target_h:
                        scale = min(target_w / w, target_h / h)
                        new_w = int(w * scale)
                        new_h = int(h * scale)
                        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

                    # Encode to JPEG
                    _, buffer = cv2.imencode('.jpg', frame,
                                             [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                    frame_data = buffer.tobytes()

                    # Compress with zstd
                    compressed = self.compressor.compress(frame_data)

                    # Send to all clients
                    self._broadcast(compressed)

                except Exception as e:
                    log.debug("Capture error: %s", e)

                elapsed = time.perf_counter() - loop_start
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

    def _broadcast(self, data):
        dead = []
        with self.clients_lock:
            for client in self.clients:
                if not send_frame(client, data):
                    dead.append(client)

            for dc in dead:
                try:
                    dc.close()
                except Exception:
                    pass
                self.clients.remove(dc)
                log.info("Screen viewer disconnected")

    def get_client_count(self):
        with self.clients_lock:
            return len(self.clients)


class ScreenReceiver:
    """Receives screen stream from broadcaster."""

    def __init__(self, host, port=SCREEN_PORT):
        self.host = host
        self.port = port
        self.running = False
        self.connected = False
        self.frame = None
        self.frame_lock = threading.Lock()
        self.decompressor = zstd.ZstdDecompressor()
        self.on_disconnect = None  # Callback

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _receive_loop(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((self.host, self.port))
            sock.settimeout(RECV_TIMEOUT)
            self.connected = True
            log.info("Connected to screen at %s:%d", self.host, self.port)

            while self.running:
                compressed = recv_frame(sock)

                # Decompress
                frame_data = self.decompressor.decompress(compressed)

                # Decode JPEG
                nparr = np.frombuffer(frame_data, np.uint8)
                frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

                if frame is not None:
                    with self.frame_lock:
                        self.frame = frame

        except ConnectionError as e:
            log.warning("Screen connection lost: %s", e)
        except Exception as e:
            log.error("Screen receiver error: %s", e)
        finally:
            self.connected = False
            self.running = False
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            if self.on_disconnect:
                self.on_disconnect()

    def get_frame(self):
        with self.frame_lock:
            return self.frame
