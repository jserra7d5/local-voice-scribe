"""Clipboard integration using xclip/xsel with read-back verification."""

import shutil
import subprocess
import time


def _verify_with_xclip(expected: str) -> bool:
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return result.stdout == expected
    except (subprocess.SubprocessError, OSError):
        return False


def _verify_with_xsel(expected: str) -> bool:
    try:
        result = subprocess.run(
            ["xsel", "--clipboard", "--output"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return result.stdout == expected
    except (subprocess.SubprocessError, OSError):
        return False


def copy_to_clipboard(text: str, timeout_s: float = 1.0) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    candidates = [
        (["xclip", "-selection", "clipboard"], _verify_with_xclip),
        (["xsel", "--clipboard", "--input"], _verify_with_xsel),
    ]

    for cmd, verify in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            continue

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if verify(text):
                return True
            time.sleep(0.05)
    return False
