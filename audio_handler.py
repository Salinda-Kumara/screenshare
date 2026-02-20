"""Audio capture and streaming using PyAudioWPatch (bundled PortAudio)."""

import threading
import socket
import logging

try:
    import pyaudiowpatch as pyaudio  # type: ignore[reportMissingImports]
except ImportError:
    import pyaudio  # type: ignore[reportMissingModuleSource]
import numpy as np
import zstandard as zstd

from config import (
    AUDIO_PORT, AUDIO_RATE, AUDIO_CHANNELS,
    AUDIO_CHUNK, AUDIO_FORMAT_WIDTH,
    CONNECT_TIMEOUT, RECV_TIMEOUT
)
from network_utils import configure_socket, send_frame, recv_frame

log = logging.getLogger(__name__)


class AudioCapture:
    """Captures audio input and streams to viewers."""

    def __init__(self, port=AUDIO_PORT, device_index=None):
        self.port = port
        self.device_index = device_index
        self.running = False
        self.clients = []
        self.clients_lock = threading.Lock()
        self.compressor = zstd.ZstdCompressor(level=1)
        self._server_sock = None

    @staticmethod
    def get_audio_devices():
        """List available audio input devices."""
        p = pyaudio.PyAudio()
        devices = []
        for i in range(p.get_device_count()):
            try:
                info = p.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    devices.append({
                        "index": i,
                        "name": info["name"],
                        "channels": int(info["maxInputChannels"]),
                        "rate": int(info["defaultSampleRate"])
                    })
            except Exception:
                continue
        p.terminate()
        return devices

    def start(self):
        self.running = True
        self._accept_thread = threading.Thread(target=self._accept_connections, daemon=True)
        self._accept_thread.start()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        log.info("Audio capture started on port %d", self.port)

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
        log.info("Audio capture stopped")

    def _accept_connections(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        configure_socket(self._server_sock, is_server=True)
        self._server_sock.settimeout(1.0)
        try:
            self._server_sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            log.error("Cannot bind audio port %d: %s", self.port, e)
            self.running = False
            return
        self._server_sock.listen(10)

        while self.running:
            try:
                client, addr = self._server_sock.accept()
                configure_socket(client)
                with self.clients_lock:
                    self.clients.append(client)
                log.info("Audio viewer connected from %s", addr[0])
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    continue
                break

    def _capture_loop(self):
        p = pyaudio.PyAudio()
        stream = None

        try:
            stream = p.open(
                format=p.get_format_from_width(AUDIO_FORMAT_WIDTH),
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=AUDIO_CHUNK
            )
            log.info("Audio stream opened (device=%s, rate=%d, channels=%d)",
                     self.device_index, AUDIO_RATE, AUDIO_CHANNELS)

            while self.running:
                try:
                    audio_data = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                    compressed = self.compressor.compress(audio_data)

                    dead = []
                    with self.clients_lock:
                        for client in self.clients:
                            if not send_frame(client, compressed):
                                dead.append(client)
                        for dc in dead:
                            try:
                                dc.close()
                            except Exception:
                                pass
                            self.clients.remove(dc)

                except IOError:
                    continue
                except Exception as e:
                    log.debug("Audio capture error: %s", e)
                    if not self.running:
                        break

        except Exception as e:
            log.error("Failed to open audio stream: %s", e)
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            p.terminate()


class AudioReceiver:
    """Receives audio stream and plays it."""

    def __init__(self, host, port=AUDIO_PORT):
        self.host = host
        self.port = port
        self.running = False
        self.connected = False
        self.decompressor = zstd.ZstdDecompressor()
        self.volume = 1.0
        self.muted = False
        self.on_disconnect = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def set_volume(self, volume):
        self.volume = max(0.0, min(1.0, volume))

    def set_muted(self, muted):
        self.muted = muted

    def _receive_loop(self):
        p = pyaudio.PyAudio()
        stream = None
        sock = None

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((self.host, self.port))
            sock.settimeout(RECV_TIMEOUT)
            self.connected = True
            log.info("Connected to audio at %s:%d", self.host, self.port)

            stream = p.open(
                format=p.get_format_from_width(AUDIO_FORMAT_WIDTH),
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                output=True,
                frames_per_buffer=AUDIO_CHUNK
            )

            while self.running:
                compressed = recv_frame(sock)
                audio_data = self.decompressor.decompress(compressed)

                if self.muted:
                    continue

                # Apply volume
                if self.volume < 0.99:
                    samples = np.frombuffer(audio_data, dtype=np.int16).copy()
                    samples = (samples * self.volume).astype(np.int16)
                    audio_data = samples.tobytes()

                stream.write(audio_data)

        except ConnectionError as e:
            log.warning("Audio connection lost: %s", e)
        except Exception as e:
            log.error("Audio receiver error: %s", e)
        finally:
            self.connected = False
            self.running = False
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            p.terminate()
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            if self.on_disconnect:
                self.on_disconnect()
