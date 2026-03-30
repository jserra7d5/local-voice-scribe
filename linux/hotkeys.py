"""Global hotkey manager — KDE via qdbus CLI + dbus-monitor, pynput fallback."""

import os
import shutil
import subprocess
import threading
from typing import Callable

# Check if KDE kglobalaccel is available via qdbus
_HAS_QDBUS = shutil.which("qdbus") is not None
_HAS_DBUS_MONITOR = shutil.which("dbus-monitor") is not None
_USE_KDE = _HAS_QDBUS and _HAS_DBUS_MONITOR

# Fallback
_USE_PYNPUT = False
if not _USE_KDE:
    try:
        from pynput import keyboard
        from pynput.keyboard import Key, KeyCode
        _USE_PYNPUT = True
    except ImportError:
        pass


# ─── Qt key code mapping ───

_QT_KEY_MAP = {
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4a,
    "k": 0x4b, "l": 0x4c, "m": 0x4d, "n": 0x4e, "o": 0x4f,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5a,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "space": 0x20, "return": 0x01000004, "enter": 0x01000004,
    "escape": 0x01000000, "tab": 0x01000001,
}
_QT_MOD_MAP = {
    "shift": 0x02000000,
    "ctrl": 0x04000000,
    "alt": 0x08000000,
    "super": 0x10000000,
}


def _combo_to_qt_keycode(combo_str: str) -> int:
    parts = combo_str.lower().replace("<", "").replace(">", "").split("+")
    modifiers = 0
    key = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part in ("ctrl", "control"):
            modifiers |= _QT_MOD_MAP["ctrl"]
        elif part == "shift":
            modifiers |= _QT_MOD_MAP["shift"]
        elif part in ("alt", "option"):
            modifiers |= _QT_MOD_MAP["alt"]
        elif part in ("super", "win", "cmd", "meta", "windows"):
            modifiers |= _QT_MOD_MAP["super"]
        elif part in _QT_KEY_MAP:
            key = _QT_KEY_MAP[part]
        elif len(part) == 1:
            key = ord(part.upper())
    return modifiers | key


# ─── KDE implementation via qdbus CLI + dbus-monitor ───

class _KDEHotkeyManager:
    """Register shortcuts via qdbus, listen via dbus-monitor. No Python D-Bus binding needed."""

    COMPONENT = "local-voice-scribe"
    COMPONENT_PATH = "/component/local_voice_scribe"

    def __init__(self):
        self._callbacks: dict[str, Callable] = {}
        self._monitor_proc: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None

    def register(self, combo: str, action_name: str, callback: Callable):
        qt_key = _combo_to_qt_keycode(combo)
        self._callbacks[action_name] = callback

        # Register via qdbus using invokeShortcut-compatible names
        # First, register the action
        subprocess.run([
            "qdbus", "org.kde.kglobalaccel", "/kglobalaccel",
            "org.kde.KGlobalAccel.doRegister",
            self.COMPONENT, action_name, "Local Voice Scribe", action_name,
        ], capture_output=True, timeout=5)

        # Set the shortcut key
        subprocess.run([
            "qdbus", "org.kde.kglobalaccel", "/kglobalaccel",
            "org.kde.KGlobalAccel.setForeignShortcut",
            self.COMPONENT, action_name, "Local Voice Scribe", action_name,
            str(qt_key),
        ], capture_output=True, timeout=5)

    def start(self):
        """Start dbus-monitor to listen for globalShortcutPressed signals."""
        self._monitor_proc = subprocess.Popen(
            [
                "dbus-monitor", "--session",
                f"type='signal',interface='org.kde.kglobalaccel.Component',"
                f"member='globalShortcutPressed',"
                f"path='{self.COMPONENT_PATH}'",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._monitor_thread = threading.Thread(target=self._read_signals, daemon=True)
        self._monitor_thread.start()

    def stop(self):
        if self._monitor_proc:
            self._monitor_proc.terminate()
            self._monitor_proc = None
        self._unregister_all()

    def _read_signals(self):
        """Parse dbus-monitor output for shortcut activations."""
        proc = self._monitor_proc
        if not proc or not proc.stdout:
            return

        # dbus-monitor outputs signal blocks like:
        #   signal time=... sender=... -> dest=... path=/component/local_voice_scribe
        #     interface=org.kde.kglobalaccel.Component member=globalShortcutPressed
        #     string "local-voice-scribe"
        #     string "super_alt_r"
        #     int64 1234567890
        collecting = False
        action_name = None

        for line in proc.stdout:
            line = line.strip()
            if "member=globalShortcutPressed" in line:
                collecting = True
                action_name = None
                continue

            if collecting and line.startswith("string "):
                # Extract the string value between quotes
                val = line.split('"')[1] if '"' in line else ""
                if action_name is None:
                    # First string is component name, skip it
                    action_name = ""  # sentinel
                elif action_name == "":
                    # Second string is the action name
                    action_name = val
                    collecting = False
                    cb = self._callbacks.get(action_name)
                    if cb:
                        threading.Thread(target=cb, daemon=True).start()

            if not line and collecting:
                # Empty line = end of signal block
                collecting = False

    def _unregister_all(self):
        for action_name in self._callbacks:
            try:
                subprocess.run([
                    "qdbus", "org.kde.kglobalaccel", "/kglobalaccel",
                    "org.kde.KGlobalAccel.setInactive",
                    self.COMPONENT, action_name, "Local Voice Scribe", action_name,
                ], capture_output=True, timeout=5)
            except Exception:
                pass


# ─── pynput fallback ───

class _PynputHotkeyManager:
    """Fallback global hotkey listener using pynput (may leak keystrokes on X11)."""

    def __init__(self):
        self._hotkeys: dict[frozenset[str], tuple[str, Callable]] = {}
        self._current_keys: set[str] = set()
        self._listener = None
        self._lock = threading.Lock()

    def register(self, combo: str, action_name: str, callback: Callable):
        keys = self._parse_combo(combo)
        with self._lock:
            self._hotkeys[frozenset(keys)] = (action_name, callback)

    def start(self):
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        normalized = self._normalize_key(key)
        if normalized:
            with self._lock:
                self._current_keys.add(normalized)
                current = frozenset(self._current_keys)
                for combo, (name, callback) in self._hotkeys.items():
                    if combo == current:
                        self._current_keys.clear()
                        threading.Thread(target=callback, daemon=True).start()
                        break

    def _on_release(self, key):
        normalized = self._normalize_key(key)
        if normalized:
            with self._lock:
                self._current_keys.discard(normalized)

    def _normalize_key(self, key) -> str | None:
        if isinstance(key, Key):
            name = key.name.lower()
            if name in ("ctrl_l", "ctrl_r", "ctrl"): return "ctrl"
            elif name in ("shift_l", "shift_r", "shift"): return "shift"
            elif name in ("alt_l", "alt_r", "alt", "alt_gr"): return "alt"
            elif name in ("cmd_l", "cmd_r", "cmd", "super_l", "super_r", "super"): return "super"
            return name
        elif isinstance(key, KeyCode):
            if key.char: return key.char.lower()
            elif key.vk: return f"vk_{key.vk}"
        return None

    def _parse_combo(self, combo_str: str) -> set[str]:
        keys = set()
        for part in combo_str.lower().replace("<", "").replace(">", "").split("+"):
            part = part.strip()
            if not part: continue
            if part in ("ctrl", "control"): keys.add("ctrl")
            elif part == "shift": keys.add("shift")
            elif part in ("alt", "option"): keys.add("alt")
            elif part in ("super", "win", "cmd", "meta", "windows"): keys.add("super")
            else: keys.add(part)
        return keys


# ─── Public API ───

class HotkeyManager:
    """Unified hotkey manager — uses KDE kglobalaccel if available, pynput otherwise."""

    def __init__(self):
        if _USE_KDE:
            self._backend = _KDEHotkeyManager()
            self._backend_name = "kde-dbus"
        elif _USE_PYNPUT:
            self._backend = _PynputHotkeyManager()
            self._backend_name = "pynput"
        else:
            self._backend = None
            self._backend_name = "none"

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def register(self, combo: str, callback: Callable):
        if self._backend:
            action_name = combo.replace("+", "_")
            self._backend.register(combo, action_name, callback)

    def start(self):
        if self._backend:
            self._backend.start()

    def stop(self):
        if self._backend:
            self._backend.stop()


def format_hotkey(combo: str) -> str:
    """Format 'super+alt+r' as 'Super+Alt+R' for display."""
    parts = combo.lower().replace("<", "").replace(">", "").split("+")
    formatted = []
    for part in parts:
        part = part.strip()
        if part in ("ctrl", "control"): formatted.append("Ctrl")
        elif part == "shift": formatted.append("Shift")
        elif part in ("alt", "option"): formatted.append("Alt")
        elif part in ("super", "win", "cmd", "meta"): formatted.append("Super")
        else: formatted.append(part.upper() if len(part) == 1 else part.capitalize())
    return "+".join(formatted)
