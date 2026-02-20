"""CLI Viewer - receives and displays screen and audio from broadcaster."""

import sys
import time
import logging

import cv2

from screen_handler import ScreenReceiver
from audio_handler import AudioReceiver
from discovery import DiscoveryClient
from config import SCREEN_PORT, AUDIO_PORT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main():
    print("=" * 50)
    print("  LAN Screen Share - Viewer (CLI)")
    print("=" * 50)

    print("\nSearching for broadcasters on LAN...")
    broadcasters = DiscoveryClient.discover(timeout=3)

    host = None
    screen_port = SCREEN_PORT
    audio_port = AUDIO_PORT

    if broadcasters:
        print(f"\nFound {len(broadcasters)} broadcaster(s):")
        for i, b in enumerate(broadcasters):
            print(f"  [{i}] {b['name']} ({b['ip']})")

        if len(broadcasters) == 1:
            selected = broadcasters[0]
            print(f"\nAuto-selecting: {selected['name']} ({selected['ip']})")
        else:
            try:
                choice = int(input("\nSelect broadcaster number: "))
                selected = broadcasters[choice]
            except (ValueError, IndexError, EOFError):
                selected = broadcasters[0]

        host = selected["ip"]
        screen_port = selected.get("screen_port", SCREEN_PORT)
        audio_port = selected.get("audio_port", AUDIO_PORT)
    else:
        print("\nNo broadcasters found automatically.")
        try:
            host = input("Enter broadcaster IP: ").strip()
        except EOFError:
            print("No input. Exiting.")
            return

    if not host:
        print("No host specified. Exiting.")
        return

    print(f"\nConnecting to {host}...")

    screen_receiver = ScreenReceiver(host, screen_port)
    audio_receiver = AudioReceiver(host, audio_port)

    screen_receiver.start()
    audio_receiver.start()

    print("Connected! Displaying screen...")
    print("Press 'Q' in the window or Ctrl+C to disconnect.\n")

    window_name = f"Screen Share - {host}"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, 1280, 720)

    try:
        while screen_receiver.running:
            frame = screen_receiver.get_frame()
            if frame is not None:
                cv2.imshow(window_name, frame)

            key = cv2.waitKey(16) & 0xFF  # ~60fps display check
            if key == ord('q') or key == ord('Q') or key == 27:  # Q or ESC
                break

            try:
                if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break
    except KeyboardInterrupt:
        pass

    print("Disconnecting...")
    screen_receiver.stop()
    audio_receiver.stop()
    cv2.destroyAllWindows()
    print("Done. Goodbye!")


if __name__ == "__main__":
    main()
