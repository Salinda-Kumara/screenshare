"""Reliable network send/receive helpers with magic header validation."""

import struct
import socket
import logging

from config import MAGIC, HEADER_FORMAT, HEADER_SIZE, SOCKET_BUFFER

log = logging.getLogger(__name__)


def configure_socket(sock, is_server=False):
    """Apply reliable socket options."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
    if is_server:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Enable keepalive to detect dead connections
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


def send_frame(sock, data: bytes) -> bool:
    """
    Send a frame with magic header + length prefix.
    Returns True on success, False on failure.
    """
    try:
        header = struct.pack(HEADER_FORMAT, MAGIC, len(data))
        sock.sendall(header + data)
        return True
    except (OSError, BrokenPipeError, ConnectionResetError) as e:
        log.debug("send_frame failed: %s", e)
        return False


def recv_frame(sock) -> bytes:
    """
    Receive a frame with magic header validation.
    Returns frame bytes or raises ConnectionError.
    """
    # Read header
    header = _recv_exact(sock, HEADER_SIZE)
    magic, length = struct.unpack(HEADER_FORMAT, header)

    if magic != MAGIC:
        raise ConnectionError(f"Invalid magic bytes: {magic!r}")

    if length > 50_000_000:  # 50MB max frame sanity check
        raise ConnectionError(f"Frame too large: {length}")

    if length == 0:
        return b""

    return _recv_exact(sock, length)


def _recv_exact(sock, size: int) -> bytes:
    """Receive exactly 'size' bytes from socket."""
    buf = bytearray()
    while len(buf) < size:
        remaining = size - len(buf)
        try:
            chunk = sock.recv(min(remaining, SOCKET_BUFFER))
        except socket.timeout:
            raise ConnectionError("Receive timed out")
        except OSError as e:
            raise ConnectionError(f"Socket error: {e}")
        if not chunk:
            raise ConnectionError("Connection closed by remote")
        buf.extend(chunk)
    return bytes(buf)
