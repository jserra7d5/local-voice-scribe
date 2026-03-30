"""Desktop notifications using notify-send."""

import shutil
import subprocess


def notify(message: str, title: str = "Local Voice Scribe", timeout_ms: int = 3000):
    """Send a desktop notification."""
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(
                ["notify-send", "-a", "Local Voice Scribe", "-t", str(timeout_ms), title, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
