"""Configuration constants for LAN Screen Share."""

# Network
DISCOVERY_PORT = 9876
SCREEN_PORT = 9877
AUDIO_PORT = 9878
CONTROL_PORT = 9879       # Control/signaling channel
BROADCAST_ADDR = "255.255.255.255"
DISCOVERY_MSG = b"LANSHARE_DISCOVER"
DISCOVERY_RESPONSE_PREFIX = b"LANSHARE_HERE:"

# Client roles sent on connect (first byte)
ROLE_VIEWER = b"\x01"
ROLE_SHARER = b"\x02"

# Control messages (JSON over control channel)
MSG_SHARER_LIST = "sharer_list"      # Server → Client: list of active sharers
MSG_START_SHARE = "start_share"      # Client → Server: I want to share
MSG_STOP_SHARE = "stop_share"        # Client → Server: I stop sharing
MSG_SELECT_SHARER = "select_sharer"  # Client → Server: projection selects who to view
MSG_KICK_SHARER = "kick_sharer"      # Server → Client: you've been replaced
MSG_SERVER_INFO = "server_info"      # Server → Client: server status

# Screen capture
SCREEN_FPS = 60
SCREEN_QUALITY = 100  # JPEG quality (1-100) – maximum quality
SCREEN_RESIZE_FACTOR = 1.0  # 1.0 = full resolution
VIDEO_BITRATE = 8000  # Target bitrate in kbps (higher for LAN clarity)

# Audio
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2  # Stereo for full quality
AUDIO_CHUNK = 4096
AUDIO_FORMAT_WIDTH = 2  # 16-bit audio

# WebRTC (browser ↔ browser path — lowest latency, HW-accelerated codec)
WEBRTC_ENABLED = True
WEBRTC_VIDEO_BITRATE = 8_000_000   # 8 Mbps — generous for LAN
WEBRTC_MAX_BITRATE = 15_000_000    # 15 Mbps cap
WEBRTC_MIN_BITRATE = 2_000_000     # 2 Mbps floor
WEBRTC_PREFERRED_CODECS = ["video/AV1", "video/H264", "video/VP9"]  # AV1 > H264 > VP9
WEBRTC_AUDIO_BITRATE = 128_000     # 128 kbps Opus stereo
WEBRTC_ICE_SERVERS = []            # Empty = LAN-only (no STUN/TURN needed)

# Protocol header: 4 bytes magic + 4 bytes length
MAGIC = b"LSHR"
HEADER_FORMAT = "!4sI"  # magic(4) + length(4)
HEADER_SIZE = 8

# Socket
SOCKET_BUFFER = 131072  # 128KB
RECV_TIMEOUT = 15.0
CONNECT_TIMEOUT = 5.0

# Web viewer
WEB_PORT = 1000
WEBRTC_SIGNALING_PATH = "/ws/rtc"  # WebSocket path for WebRTC signaling
