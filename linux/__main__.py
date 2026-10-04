"""Entry point: python3 -m linux"""

import os

# Under Wayland, Qt's native windows cannot skip activation, so the border
# overlay steals focus from the field being typed in. XWayland honours the
# overlay's bypass-WM hint. (Hotkeys use the GlobalShortcuts portal, not X11.)
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from .daemon import Daemon  # noqa: E402


def main():
    daemon = Daemon()
    daemon.run()


if __name__ == "__main__":
    main()
