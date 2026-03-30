"""Clipboard integration using xclip."""

import shutil
import subprocess


def copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False
