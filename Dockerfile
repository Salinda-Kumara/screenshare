# ─────────────────────────────────────────────────────────────
#  LAN Screen Share — Relay Server (Docker)
#
#  Central relay for meeting rooms.  Clients connect as sharers
#  or viewers; the server forwards the active sharer's stream.
#
#  Build:
#    docker build -t lanscreenshare .
#
#  Run:
#    docker run --rm -it --net=host lanscreenshare
#
#  Ports (if not using --net=host):
#    docker run --rm -it \
#      -p 9876:9876/udp \
#      -p 9877:9877 \
#      -p 9878:9878 \
#      -p 9879:9879 \
#      lanscreenshare
# ─────────────────────────────────────────────────────────────

FROM python:3.12-slim

LABEL maintainer="LAN Screen Share"
LABEL description="LAN Screen Share relay server for meeting rooms"

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Only need Python + zstandard (no GUI/audio/capture deps on server)
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# Copy server code
COPY config.py .
COPY network_utils.py .
COPY relay_server.py .

# Expose ports: discovery(UDP), screen(TCP), audio(TCP), control(TCP)
EXPOSE 9876/udp
EXPOSE 9877
EXPOSE 9878
EXPOSE 9879

CMD ["python", "relay_server.py"]
