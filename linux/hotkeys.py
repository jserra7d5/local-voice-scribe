"""Global hotkey manager using pynput (no Qt dependency)."""

import threading
from typing import Callable

from pynput import keyboard
from pynput.keyboard import Key, KeyCode


class HotkeyManager:
    """System-wide hotkey listener using pynput."""

    def __init__(self):
        self._hotkeys: dict[frozenset[str], Callable] = {}
        self._current_keys: set[str] = set()
        self._listener: keyboard.Listener | None = None
        self._lock = threading.Lock()
        self._running = False

    def register(self, combo: str, callback: Callable):
        """Register a hotkey combo string (e.g. 'super+alt+r') with a callback."""
        keys = self._parse_combo(combo)
        with self._lock:
            self._hotkeys[frozenset(keys)] = callback

    def start(self):
        """Start the global hotkey listener (daemon thread)."""
        if self._running:
            return
        self._running = True
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        """Stop the listener."""
        self._running = False
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        normalized = self._normalize_key(key)
        if normalized:
            with self._lock:
                self._current_keys.add(normalized)
                self._check_hotkeys()

    def _on_release(self, key):
        normalized = self._normalize_key(key)
        if normalized:
            with self._lock:
                self._current_keys.discard(normalized)

    def _check_hotkeys(self):
        current = frozenset(self._current_keys)
        for combo, callback in self._hotkeys.items():
            if combo == current:
                self._current_keys.clear()
                # Fire callback in a separate thread to avoid blocking the listener
                threading.Thread(target=callback, daemon=True).start()
                break

    def _normalize_key(self, key) -> str | None:
        if isinstance(key, Key):
            name = key.name.lower()
            if name in ("ctrl_l", "ctrl_r", "ctrl"):
                return "ctrl"
            elif name in ("shift_l", "shift_r", "shift"):
                return "shift"
            elif name in ("alt_l", "alt_r", "alt", "alt_gr"):
                return "alt"
            elif name in ("cmd_l", "cmd_r", "cmd", "super_l", "super_r", "super"):
                return "super"
            return name
        elif isinstance(key, KeyCode):
            if key.char:
                return key.char.lower()
            elif key.vk:
                return f"vk_{key.vk}"
        return None

    def _parse_combo(self, combo_str: str) -> set[str]:
        keys = set()
        parts = combo_str.lower().replace("<", "").replace(">", "").split("+")
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part in ("ctrl", "control"):
                keys.add("ctrl")
            elif part == "shift":
                keys.add("shift")
            elif part in ("alt", "option"):
                keys.add("alt")
            elif part in ("super", "win", "cmd", "meta", "windows"):
                keys.add("super")
            else:
                keys.add(part)
        return keys


def format_hotkey(combo: str) -> str:
    """Format 'super+alt+r' as 'Super+Alt+R' for display."""
    parts = combo.lower().replace("<", "").replace(">", "").split("+")
    formatted = []
    for part in parts:
        part = part.strip()
        if part in ("ctrl", "control"):
            formatted.append("Ctrl")
        elif part == "shift":
            formatted.append("Shift")
        elif part in ("alt", "option"):
            formatted.append("Alt")
        elif part in ("super", "win", "cmd", "meta"):
            formatted.append("Super")
        else:
            formatted.append(part.upper() if len(part) == 1 else part.capitalize())
    return "+".join(formatted)
