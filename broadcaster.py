"""CLI Broadcaster - shares screen and audio on LAN."""

import os
import sys
import time
import socket
import logging

from screen_handler import ScreenCapture
from audio_handler import AudioCapture
from discovery import DiscoveryBroadcaster
from config import SCREEN_PORT, AUDIO_PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def is_headless():
    """Detect if running in Docker / headless mode."""
    return (
        os.environ.get("DISPLAY") == ":99"  # Xvfb
        or os.path.exists("/.dockerenv")
        or os.environ.get("LANSHARE_HEADLESS") == "1"
    )


def main():
    headless = is_headless()

    print("=" * 50)
    print("  LAN Screen Share - Broadcaster" + (" (Docker)" if headless else " (CLI)"))
    print("=" * 50)

    local_ip = get_local_ip()
    print(f"\nYour IP: {local_ip}")
    print(f"Screen port: {SCREEN_PORT}")
    print(f"Audio port:  {AUDIO_PORT}")

    # List audio devices
    devices = AudioCapture.get_audio_devices()

    device_index = None
    if devices and not headless:
        print("\nAvailable audio input devices:")
        for d in devices:
            print(f"  [{d['index']}] {d['name']} (ch={d['channels']}, rate={d['rate']})")
        try:
            choice = input("\nSelect audio device index (Enter for default): ").strip()
            if choice:
                device_index = int(choice)
        except (ValueError, EOFError):
            pass
    elif devices:
        print(f"\nHeadless mode: using default audio device")
    else:
        print("\nNo audio input devices found. Audio will be disabled.")

    # Start services
    hostname = socket.gethostname()
    discovery = DiscoveryBroadcaster(SCREEN_PORT, AUDIO_PORT, name=hostname)
    screen_capture = ScreenCapture(port=SCREEN_PORT)

    audio_capture = None
    if devices:
        audio_capture = AudioCapture(port=AUDIO_PORT, device_index=device_index)

    print("\nStarting broadcast...")
    discovery.start()
    screen_capture.start()
    if audio_capture:
        audio_capture.start()

    print(f"\n{'=' * 50}")
    print(f"  Broadcasting on {local_ip}")
    print(f"  Viewers can auto-discover or use the IP above.")
    print(f"  Press Ctrl+C to stop.")
    print(f"{'=' * 50}\n")

    try:
        while True:
            count = screen_capture.get_client_count()
            print(f"\r  Connected viewers: {count}    ", end="", flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n\nStopping broadcast...")
        discovery.stop()
        screen_capture.stop()
        if audio_capture:
            audio_capture.stop()
        print("Stopped. Goodbye!")


if __name__ == "__main__":
    main()
