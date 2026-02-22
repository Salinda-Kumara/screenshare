"""
GUI Application for LAN Screen Share — Meeting Hub Mode.

Flow:
  1. Connect to the relay server (auto-discover or enter IP).
  2. Choose role:
     - "Share My Screen" → captures screen+audio, sends to server.
     - "View Projection" → receives stream, displays fullscreen.
  3. Projection PC can switch between sharers via control panel.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import socket
import json
import logging
import time

import cv2
import numpy as np
from PIL import Image, ImageTk

from config import (
    SCREEN_PORT, AUDIO_PORT, CONTROL_PORT,
    SCREEN_QUALITY, SCREEN_RESIZE_FACTOR,
    AUDIO_RATE, AUDIO_CHANNELS, AUDIO_CHUNK, AUDIO_FORMAT_WIDTH,
    ROLE_VIEWER, ROLE_SHARER,
    MSG_SHARER_LIST, MSG_SELECT_SHARER,
    CONNECT_TIMEOUT, RECV_TIMEOUT,
)
from network_utils import configure_socket, send_frame, recv_frame
from discovery import DiscoveryClient

import mss
import zstandard as zstd

try:
    import pyaudiowpatch as pyaudio  # type: ignore[reportMissingImports]
except ImportError:
    import pyaudio  # type: ignore[reportMissingModuleSource]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ═══════════════════════════════════════════════════════
#  SCREEN SENDER  (client → relay server)
# ═══════════════════════════════════════════════════════

class ScreenSender:
    """Captures local screen and sends to relay server."""

    def __init__(self, host, port, sharer_name,
                 quality=SCREEN_QUALITY, resize=SCREEN_RESIZE_FACTOR):
        self.host, self.port = host, port
        self.sharer_name = sharer_name
        self.quality, self.resize = quality, resize
        self.running = False
        self.sharer_id = None
        self.compressor = zstd.ZstdCompressor(level=1)

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _run(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((self.host, self.port))
            sock.settimeout(None)

            # Send role
            sock.sendall(ROLE_SHARER)

            # Send sharer info (first frame)
            info = json.dumps({"name": self.sharer_name}).encode("utf-8")
            send_frame(sock, info)

            # Wait a tiny bit for server to register, then read back our sharer_id
            # (We don't get an explicit id back — the server assigns it internally.
            #  We'll use the control channel to learn our id.)

            with mss.mss() as sct:
                monitor = sct.monitors[0]
                while self.running:
                    screenshot = sct.grab(monitor)
                    frame = np.array(screenshot)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                    if self.resize != 1.0:
                        h, w = frame.shape[:2]
                        frame = cv2.resize(frame,
                                           (int(w * self.resize), int(h * self.resize)),
                                           interpolation=cv2.INTER_LINEAR)

                    _, buf = cv2.imencode('.jpg', frame,
                                          [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                    compressed = self.compressor.compress(buf.tobytes())

                    if not send_frame(sock, compressed):
                        break

        except Exception as e:
            log.error("ScreenSender error: %s", e)
        finally:
            self.running = False
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════
#  AUDIO SENDER  (client → relay server)
# ═══════════════════════════════════════════════════════

class AudioSender:
    """Captures local audio and sends to relay server."""

    def __init__(self, host, port, sharer_name, sharer_id=None, device_index=None):
        self.host, self.port = host, port
        self.sharer_name = sharer_name
        self.sharer_id = sharer_id
        self.device_index = device_index
        self.running = False
        self.compressor = zstd.ZstdCompressor(level=1)

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _run(self):
        sock = None
        p = pyaudio.PyAudio()
        stream = None

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((self.host, self.port))
            sock.settimeout(None)

            sock.sendall(ROLE_SHARER)

            info = json.dumps({
                "name": self.sharer_name,
                "sharer_id": self.sharer_id or -1,
            }).encode("utf-8")
            send_frame(sock, info)

            stream = p.open(
                format=p.get_format_from_width(AUDIO_FORMAT_WIDTH),
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=AUDIO_CHUNK,
            )

            while self.running:
                audio_data = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                compressed = self.compressor.compress(audio_data)
                if not send_frame(sock, compressed):
                    break

        except Exception as e:
            log.error("AudioSender error: %s", e)
        finally:
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


# ═══════════════════════════════════════════════════════
#  SCREEN RECEIVER  (relay server → client viewer)
# ═══════════════════════════════════════════════════════

class ScreenViewerClient:
    """Connects to relay server as viewer, receives screen frames."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.running = False
        self.frame = None
        self.frame_lock = threading.Lock()
        self.decompressor = zstd.ZstdDecompressor()
        self.on_disconnect = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def get_frame(self):
        with self.frame_lock:
            return self.frame

    def _run(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((self.host, self.port))

            sock.sendall(ROLE_VIEWER)
            sock.settimeout(RECV_TIMEOUT)

            while self.running:
                compressed = recv_frame(sock)
                data = self.decompressor.decompress(compressed)
                nparr = np.frombuffer(data, np.uint8)
                frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if frame is not None:
                    with self.frame_lock:
                        self.frame = frame

        except ConnectionError as e:
            log.warning("Screen viewer lost: %s", e)
        except Exception as e:
            log.error("Screen viewer error: %s", e)
        finally:
            self.running = False
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            if self.on_disconnect:
                self.on_disconnect()


# ═══════════════════════════════════════════════════════
#  AUDIO RECEIVER
# ═══════════════════════════════════════════════════════

class AudioViewerClient:
    """Connects to relay server as viewer, plays audio."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.running = False
        self.decompressor = zstd.ZstdDecompressor()
        self.volume = 1.0
        self.muted = False

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def set_volume(self, v):
        self.volume = max(0.0, min(1.0, v))

    def set_muted(self, m):
        self.muted = m

    def _run(self):
        sock = None
        p = pyaudio.PyAudio()
        stream = None

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((self.host, self.port))

            sock.sendall(ROLE_VIEWER)
            sock.settimeout(RECV_TIMEOUT)

            stream = p.open(
                format=p.get_format_from_width(AUDIO_FORMAT_WIDTH),
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                output=True,
                frames_per_buffer=AUDIO_CHUNK,
            )

            while self.running:
                compressed = recv_frame(sock)
                audio_data = self.decompressor.decompress(compressed)

                if self.muted:
                    continue

                if self.volume < 0.99:
                    samples = np.frombuffer(audio_data, dtype=np.int16).copy()
                    samples = (samples * self.volume).astype(np.int16)
                    audio_data = samples.tobytes()

                stream.write(audio_data)

        except ConnectionError:
            pass
        except Exception as e:
            log.error("Audio viewer error: %s", e)
        finally:
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


# ═══════════════════════════════════════════════════════
#  CONTROL CLIENT  (keeps in sync with server)
# ═══════════════════════════════════════════════════════

class ControlClient:
    """Connects to server control channel. Receives sharer list updates."""

    def __init__(self, host, port=CONTROL_PORT):
        self.host, self.port = host, port
        self.running = False
        self.sharers = []
        self.active_sharer_id = None
        self.lock = threading.Lock()
        self.on_update = None  # callback
        self._sock = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def select_sharer(self, sharer_id):
        """Ask server to switch active sharer."""
        msg = json.dumps({
            "type": MSG_SELECT_SHARER,
            "sharer_id": sharer_id,
        }).encode("utf-8")
        try:
            send_frame(self._sock, msg)
        except Exception:
            pass

    def _run(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_socket(self._sock)
            self._sock.settimeout(CONNECT_TIMEOUT)
            self._sock.connect((self.host, self.port))
            self._sock.settimeout(30.0)

            while self.running:
                data = recv_frame(self._sock)
                msg = json.loads(data.decode("utf-8"))

                if msg.get("type") == MSG_SHARER_LIST:
                    with self.lock:
                        self.sharers = msg.get("sharers", [])
                        self.active_sharer_id = msg.get("active_sharer_id")
                    if self.on_update:
                        self.on_update()

        except ConnectionError:
            pass
        except Exception as e:
            log.debug("Control client error: %s", e)
        finally:
            self.running = False
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════
#  MAIN GUI APPLICATION
# ═══════════════════════════════════════════════════════

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("LAN Screen Share")
        self.root.geometry("950x680")
        self.root.minsize(700, 500)
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._scheduled = None
        self._photo_ref = None

        # Server connection
        self.server_ip = None
        self.server_info = None

        # Active components
        self.screen_sender = None
        self.audio_sender = None
        self.screen_viewer = None
        self.audio_viewer = None
        self.control_client = None
        self.sharing = False
        self.viewing = False

        self._setup_styles()
        self._build_connect_screen()

    # ───────── STYLES ─────────

    def _setup_styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("Title.TLabel", font=("Segoe UI", 22, "bold"),
                     foreground="#cdd6f4", background="#1e1e2e")
        s.configure("Sub.TLabel", font=("Segoe UI", 11),
                     foreground="#a6adc8", background="#1e1e2e")
        s.configure("StatusGreen.TLabel", font=("Segoe UI", 11),
                     foreground="#a6e3a1", background="#1e1e2e")
        s.configure("StatusRed.TLabel", font=("Segoe UI", 11),
                     foreground="#f38ba8", background="#1e1e2e")
        s.configure("Big.TButton", font=("Segoe UI", 13, "bold"), padding=18)
        s.configure("TFrame", background="#1e1e2e")
        s.configure("TLabelframe", background="#1e1e2e", foreground="#cdd6f4")
        s.configure("TLabelframe.Label", background="#1e1e2e", foreground="#cdd6f4",
                     font=("Segoe UI", 10, "bold"))

    def _clear(self):
        if self._scheduled:
            self.root.after_cancel(self._scheduled)
            self._scheduled = None
        for w in self.root.winfo_children():
            w.destroy()

    def _schedule(self, ms, func):
        self._scheduled = self.root.after(ms, func)

    # ═══════════════════════════════════════════════════
    #  SCREEN 1: CONNECT TO SERVER
    # ═══════════════════════════════════════════════════

    def _build_connect_screen(self):
        self._clear()
        self.root.title("LAN Screen Share — Connect")

        outer = ttk.Frame(self.root)
        outer.place(relx=0.5, rely=0.40, anchor="center")

        ttk.Label(outer, text="LAN Screen Share", style="Title.TLabel").pack(pady=(0, 5))
        ttk.Label(outer, text=f"Your IP: {get_local_ip()}", style="Sub.TLabel").pack(pady=(0, 25))

        # Manual IP
        manual_f = ttk.Frame(outer)
        manual_f.pack(pady=(0, 10))
        ttk.Label(manual_f, text="Server IP:", style="Sub.TLabel").pack(side="left", padx=(0, 8))
        self._ip_entry = ttk.Entry(manual_f, width=20, font=("Segoe UI", 11))
        self._ip_entry.pack(side="left", padx=(0, 8))
        ttk.Button(manual_f, text="Connect", command=self._connect_manual).pack(side="left")

        ttk.Button(outer, text="Search LAN for Server", command=self._search_servers).pack(pady=10)

        # Server list
        list_f = ttk.Frame(outer)
        list_f.pack(fill="both", expand=True, pady=5)
        self._server_lb = tk.Listbox(list_f, font=("Segoe UI", 11), bg="#313244", fg="#cdd6f4",
                                      selectbackground="#585b70", selectforeground="#cdd6f4",
                                      activestyle="none", height=5, width=50)
        self._server_lb.pack(fill="both", expand=True)
        self._server_lb.bind("<Double-1>", lambda e: self._connect_selected_server())

        btn_f = ttk.Frame(outer)
        btn_f.pack(pady=8)
        ttk.Button(btn_f, text="Connect to Selected", command=self._connect_selected_server).pack()

        self._connect_status = ttk.Label(outer, text="", style="Sub.TLabel")
        self._connect_status.pack(pady=5)

        self._found_servers = []
        self._search_servers()

    def _search_servers(self):
        self._connect_status.configure(text="Searching...")
        self._server_lb.delete(0, tk.END)
        self._found_servers = []

        def work():
            results = DiscoveryClient.discover(timeout=3)
            # Also probe localhost
            for h in ["127.0.0.1"]:
                if h not in {r.get("ip") for r in results}:
                    if self._probe(h, SCREEN_PORT):
                        results.append({"name": f"Local ({h})", "ip": h,
                                        "screen_port": SCREEN_PORT, "audio_port": AUDIO_PORT,
                                        "control_port": CONTROL_PORT, "type": "relay_server"})
            self.root.after(0, lambda: self._show_servers(results))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _probe(host, port, timeout=1.5):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            return False

    def _show_servers(self, results):
        self._found_servers = results
        self._server_lb.delete(0, tk.END)
        if results:
            for s in results:
                self._server_lb.insert(tk.END, f"   {s['name']}   —   {s['ip']}")
            self._connect_status.configure(text=f"Found {len(results)} server(s). Double-click to connect.")
        else:
            self._connect_status.configure(text="No servers found. Enter IP manually or start server with Docker.")

    def _connect_selected_server(self):
        sel = self._server_lb.curselection()
        if not sel:
            messagebox.showinfo("Select", "Select a server from the list.")
            return
        s = self._found_servers[sel[0]]
        self._do_connect(s["ip"], s)

    def _connect_manual(self):
        ip = self._ip_entry.get().strip()
        if not ip:
            messagebox.showinfo("IP", "Enter the server IP.")
            return
        self._do_connect(ip, {"ip": ip, "screen_port": SCREEN_PORT,
                               "audio_port": AUDIO_PORT, "control_port": CONTROL_PORT})

    def _do_connect(self, ip, info):
        self.server_ip = ip
        self.server_info = info

        # Connect control channel
        self.control_client = ControlClient(ip, info.get("control_port", CONTROL_PORT))
        self.control_client.on_update = lambda: self.root.after(0, self._on_sharer_update)

        try:
            self.control_client.start()
            time.sleep(0.3)
            if not self.control_client.running:
                raise ConnectionError("Control connection failed")
            self._build_lobby()
        except Exception as e:
            messagebox.showerror("Connection Failed", f"Cannot connect to {ip}:\n{e}")

    # ═══════════════════════════════════════════════════
    #  SCREEN 2: LOBBY — choose Share or View
    # ═══════════════════════════════════════════════════

    def _build_lobby(self):
        self._clear()
        self.root.title(f"Connected — {self.server_ip}")

        outer = ttk.Frame(self.root)
        outer.place(relx=0.5, rely=0.40, anchor="center")

        ttk.Label(outer, text="Connected to Server", style="Title.TLabel").pack(pady=(0, 5))
        ttk.Label(outer, text=f"Server: {self.server_ip}", style="Sub.TLabel").pack(pady=(0, 30))

        btn_row = ttk.Frame(outer)
        btn_row.pack()

        ttk.Button(btn_row, text="Share My Screen", style="Big.TButton",
                   command=self._start_sharing).grid(row=0, column=0, padx=20)
        ttk.Button(btn_row, text="View Projection", style="Big.TButton",
                   command=self._start_viewing).grid(row=0, column=1, padx=20)

        ttk.Button(outer, text="Disconnect", command=self._disconnect).pack(pady=25)

        # Show current sharers
        ttk.Label(outer, text="Currently sharing:", style="Sub.TLabel").pack(pady=(15, 5))
        self._lobby_sharer_label = ttk.Label(outer, text="(none)", style="Sub.TLabel")
        self._lobby_sharer_label.pack()
        self._refresh_lobby_sharers()

    def _refresh_lobby_sharers(self):
        if self.control_client:
            with self.control_client.lock:
                names = [s["name"] for s in self.control_client.sharers]
            if names:
                self._lobby_sharer_label.configure(text=", ".join(names))
            else:
                self._lobby_sharer_label.configure(text="(none)")

    def _on_sharer_update(self):
        """Called when control client receives updated sharer list."""
        try:
            self._refresh_lobby_sharers()
        except Exception:
            pass
        try:
            self._refresh_viewer_sharers()
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    #  SHARE MODE — send screen + audio to server
    # ═══════════════════════════════════════════════════

    def _start_sharing(self):
        self._clear()
        self.root.title("Sharing My Screen")

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=25, pady=20)

        ttk.Label(main, text="Sharing My Screen", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text=f"Server: {self.server_ip}", style="Sub.TLabel").pack(anchor="w", pady=(2, 15))

        # Settings
        settings = ttk.LabelFrame(main, text="Settings", padding=12)
        settings.pack(fill="x", pady=(0, 12))

        ttk.Label(settings, text="JPEG Quality:", style="Sub.TLabel").grid(row=0, column=0, sticky="w", padx=5, pady=4)
        self._quality_var = tk.IntVar(value=100)
        q_sc = ttk.Scale(settings, from_=10, to=100, variable=self._quality_var, orient="horizontal")
        q_sc.grid(row=0, column=1, sticky="ew", padx=5, pady=4)

        # Audio device
        ttk.Label(settings, text="Audio:", style="Sub.TLabel").grid(row=2, column=0, sticky="w", padx=5, pady=4)
        self._audio_dev_var = tk.StringVar(value="Default")
        self._audio_devices = []
        try:
            p = pyaudio.PyAudio()
            for i in range(p.get_device_count()):
                try:
                    info = p.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0:
                        self._audio_devices.append({"index": i, "name": info["name"]})
                except Exception:
                    continue
            p.terminate()
        except Exception:
            pass
        dev_names = ["Default"] + [d["name"] for d in self._audio_devices]
        ttk.Combobox(settings, textvariable=self._audio_dev_var, values=dev_names,
                      width=40, state="readonly").grid(row=2, column=1, columnspan=2,
                                                        sticky="ew", padx=5, pady=4)
        settings.columnconfigure(1, weight=1)

        # Buttons
        btn_f = ttk.Frame(main)
        btn_f.pack(pady=15)
        self._share_start_btn = ttk.Button(btn_f, text="Start Sharing", command=self._do_start_share)
        self._share_start_btn.pack(side="left", padx=6)
        self._share_stop_btn = ttk.Button(btn_f, text="Stop", command=self._do_stop_share, state="disabled")
        self._share_stop_btn.pack(side="left", padx=6)
        ttk.Button(btn_f, text="Back", command=self._back_to_lobby).pack(side="left", padx=6)

        self._share_status = ttk.Label(main, text="Ready", style="Sub.TLabel")
        self._share_status.pack(pady=10)

    def _do_start_share(self):
        hostname = socket.gethostname()
        quality = self._quality_var.get()

        dev_idx = None
        sel = self._audio_dev_var.get()
        if sel != "Default":
            for d in self._audio_devices:
                if d["name"] == sel:
                    dev_idx = d["index"]
                    break

        sp = self.server_info.get("screen_port", SCREEN_PORT)
        ap = self.server_info.get("audio_port", AUDIO_PORT)

        try:
            self.screen_sender = ScreenSender(self.server_ip, sp, hostname,
                                               quality=quality)
            self.audio_sender = AudioSender(self.server_ip, ap, hostname,
                                             device_index=dev_idx)
            self.screen_sender.start()
            self.audio_sender.start()
            self.sharing = True

            self._share_start_btn.configure(state="disabled")
            self._share_stop_btn.configure(state="normal")
            self._share_status.configure(text="Sharing...", style="StatusGreen.TLabel")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to start sharing:\n{e}")

    def _do_stop_share(self):
        self.sharing = False
        if self.screen_sender:
            self.screen_sender.stop()
            self.screen_sender = None
        if self.audio_sender:
            self.audio_sender.stop()
            self.audio_sender = None
        try:
            self._share_start_btn.configure(state="normal")
            self._share_stop_btn.configure(state="disabled")
            self._share_status.configure(text="Stopped", style="StatusRed.TLabel")
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    #  VIEW MODE — receive stream + control panel
    # ═══════════════════════════════════════════════════

    def _start_viewing(self):
        self._clear()
        self.root.title(f"Projection — {self.server_ip}")

        # Top bar
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=6)

        ttk.Label(top, text=f"Projection — {self.server_ip}", style="Sub.TLabel").pack(side="left")
        ttk.Button(top, text="Disconnect", command=self._stop_viewing).pack(side="right", padx=4)
        ttk.Button(top, text="Fullscreen (F11)", command=self._toggle_fullscreen).pack(side="right", padx=4)

        # Volume
        ttk.Label(top, text="Vol:", style="Sub.TLabel").pack(side="right")
        self._vol_var = tk.DoubleVar(value=1.0)
        ttk.Scale(top, from_=0.0, to=1.0, variable=self._vol_var,
                  orient="horizontal", length=100, command=self._on_vol).pack(side="right", padx=4)
        self._mute_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Mute", variable=self._mute_var,
                         command=self._on_mute).pack(side="right", padx=4)

        # Sharer selector panel
        ctrl_f = ttk.LabelFrame(self.root, text="Active Sharers — click to switch", padding=6)
        ctrl_f.pack(fill="x", padx=10, pady=(0, 5))

        self._sharer_list_frame = ttk.Frame(ctrl_f)
        self._sharer_list_frame.pack(fill="x")
        self._sharer_none_label = ttk.Label(self._sharer_list_frame,
                                             text="Waiting for someone to share...", style="Sub.TLabel")
        self._sharer_none_label.pack()

        # Canvas
        self._canvas = tk.Canvas(self.root, bg="#000000", highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)

        self._view_status = ttk.Label(self.root, text="Connecting...", style="Sub.TLabel")
        self._view_status.pack(fill="x", padx=10, pady=4)

        # Fullscreen toggle
        self.root.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.root.bind("<Escape>", lambda e: self._exit_fullscreen())
        self._is_fullscreen = False

        # Connect
        sp = self.server_info.get("screen_port", SCREEN_PORT)
        ap = self.server_info.get("audio_port", AUDIO_PORT)

        try:
            self.screen_viewer = ScreenViewerClient(self.server_ip, sp)
            self.audio_viewer = AudioViewerClient(self.server_ip, ap)

            self.screen_viewer.on_disconnect = lambda: self.root.after(0, self._on_view_disconnect)
            self.screen_viewer.start()
            self.audio_viewer.start()
            self.viewing = True

            self._view_status.configure(text="Connected — waiting for stream",
                                        style="StatusGreen.TLabel")
            self._render_loop()
            self._refresh_viewer_sharers()

        except Exception as e:
            self._view_status.configure(text=f"Failed: {e}", style="StatusRed.TLabel")

    def _refresh_viewer_sharers(self):
        """Update the sharer buttons panel."""
        if not self.control_client or not hasattr(self, '_sharer_list_frame'):
            return

        for w in self._sharer_list_frame.winfo_children():
            w.destroy()

        with self.control_client.lock:
            sharers = list(self.control_client.sharers)
            active_id = self.control_client.active_sharer_id

        if not sharers:
            ttk.Label(self._sharer_list_frame,
                      text="Waiting for someone to share...", style="Sub.TLabel").pack()
            return

        for s in sharers:
            sid = s["id"]
            name = s["name"]
            lbl = f"{'>>> ' if sid == active_id else ''}{name} ({s['ip']})"
            btn = ttk.Button(
                self._sharer_list_frame, text=lbl,
                command=lambda _id=sid: self._select_sharer(_id)
            )
            btn.pack(side="left", padx=4, pady=2)

    def _select_sharer(self, sharer_id):
        if self.control_client:
            self.control_client.select_sharer(sharer_id)

    def _on_vol(self, val):
        if self.audio_viewer:
            self.audio_viewer.set_volume(float(val))

    def _on_mute(self):
        if self.audio_viewer:
            self.audio_viewer.set_muted(self._mute_var.get())

    def _toggle_fullscreen(self):
        self._is_fullscreen = not self._is_fullscreen
        self.root.attributes("-fullscreen", self._is_fullscreen)

    def _exit_fullscreen(self):
        self._is_fullscreen = False
        self.root.attributes("-fullscreen", False)

    def _on_view_disconnect(self):
        if self.viewing:
            self._view_status.configure(text="Disconnected from server", style="StatusRed.TLabel")
            self.viewing = False

    def _render_loop(self):
        if not self.viewing:
            return

        frame = self.screen_viewer.get_frame() if self.screen_viewer else None
        if frame is not None:
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)

                cw = self._canvas.winfo_width()
                ch = self._canvas.winfo_height()

                if cw > 1 and ch > 1:
                    iw, ih = img.size
                    ratio = min(cw / iw, ch / ih)
                    nw, nh = int(iw * ratio), int(ih * ratio)
                    if nw > 0 and nh > 0:
                        img = img.resize((nw, nh), Image.LANCZOS)

                self._photo_ref = ImageTk.PhotoImage(img)
                if not hasattr(self, '_canvas_image_id') or self._canvas_image_id is None:
                    self._canvas_image_id = self._canvas.create_image(cw // 2, ch // 2, anchor="center", image=self._photo_ref)
                else:
                    self._canvas.coords(self._canvas_image_id, cw // 2, ch // 2)
                    self._canvas.itemconfigure(self._canvas_image_id, image=self._photo_ref)
            except Exception:
                pass

        self._schedule(33, self._render_loop)

    def _stop_viewing(self):
        self.viewing = False
        self._exit_fullscreen()
        if self.screen_viewer:
            self.screen_viewer.stop()
            self.screen_viewer = None
        if self.audio_viewer:
            self.audio_viewer.stop()
            self.audio_viewer = None
        self._build_lobby()

    # ───────── NAVIGATION ─────────

    def _back_to_lobby(self):
        self._do_stop_share()
        self._build_lobby()

    def _disconnect(self):
        self._do_stop_share()
        if self.viewing:
            self._stop_viewing()
        if self.control_client:
            self.control_client.stop()
            self.control_client = None
        self.server_ip = None
        self.server_info = None
        self._build_connect_screen()

    def _on_close(self):
        self._do_stop_share()
        if self.viewing:
            self.viewing = False
            if self.screen_viewer:
                self.screen_viewer.stop()
            if self.audio_viewer:
                self.audio_viewer.stop()
        if self.control_client:
            self.control_client.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()
