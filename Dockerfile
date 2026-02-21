# ─────────────────────────────────────────────────────────────
#  LAN Screen Share — Relay Server + Web Viewer (Docker)
#
#  Central relay for meeting rooms.  Clients connect as sharers
#  or viewers; the server forwards the active sharer's stream.
#  A built-in web viewer serves a browser UI on port 8080.
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
#      -p 1000:1000 \
#      lanscreenshare
# ─────────────────────────────────────────────────────────────

FROM python:3.12-slim

LABEL maintainer="LAN Screen Share"
LABEL description="LAN Screen Share relay server + web viewer for meeting rooms"

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Python deps: zstandard (protocol) + aiohttp (web viewer)
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# Copy server + web viewer code
COPY config.py .
COPY network_utils.py .
COPY discovery.py .
COPY relay_server.py .
COPY web_viewer.py .
COPY static/ ./static/
COPY server_entrypoint.sh .
RUN chmod +x server_entrypoint.sh

# Expose ports: discovery(UDP), screen(TCP), audio(TCP), control(TCP), web(HTTP)
EXPOSE 9876/udp
EXPOSE 9877
EXPOSE 9878
EXPOSE 9879
EXPOSE 1000

CMD ["./server_entrypoint.sh"]
