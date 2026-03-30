"""Global hotkey manager — X11 via python-xlib XGrabKey, pynput fallback."""

import os
import threading
from typing import Callable

# Try X11 native key grabbing first (most reliable on X11)
_USE_XLIB = False
try:
    from Xlib import X, XK, display as xdisplay, error as xerror
    if os.environ.get("DISPLAY"):
        _USE_XLIB = True
except ImportError:
    pass

# Fallback to pynput
_USE_PYNPUT = False
if not _USE_XLIB:
    try:
        from pynput import keyboard
        from pynput.keyboard import Key, KeyCode
        _USE_PYNPUT = True
    except ImportError:
        pass


# ─── X11 modifier and key mapping ───

_X11_MOD_MAP = {
    "shift": "Shift_L",
    "ctrl": "Control_L",
    "alt": "Alt_L",
    "super": "Super_L",
}

# X11 modifier mask bits
_X11_MOD_MASKS = {
    "shift": X.ShiftMask if _USE_XLIB else 0,
    "ctrl": X.ControlMask if _USE_XLIB else 0,
    "alt": X.Mod1Mask if _USE_XLIB else 0,      # Alt is typically Mod1
    "super": X.Mod4Mask if _USE_XLIB else 0,     # Super is typically Mod4
}

# Extra modifier masks that may be active (NumLock, CapsLock, ScrollLock)
# We need to grab with all combinations of these to catch keypresses
# regardless of lock key state
_LOCK_MASKS = [0]
if _USE_XLIB:
    _LOCK_MASKS = [
        0,
        X.LockMask,                    # CapsLock
        X.Mod2Mask,                    # NumLock (usually Mod2)
        X.LockMask | X.Mod2Mask,      # Both
    ]


def _parse_combo(combo_str: str) -> tuple[int, str]:
    """Parse 'super+alt+r' into (modifier_mask, key_name)."""
    parts = combo_str.lower().replace("<", "").replace(">", "").split("+")
    modifiers = 0
    key_name = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part in ("ctrl", "control"):
            modifiers |= _X11_MOD_MASKS.get("ctrl", 0)
        elif part == "shift":
            modifiers |= _X11_MOD_MASKS.get("shift", 0)
        elif part in ("alt", "option"):
            modifiers |= _X11_MOD_MASKS.get("alt", 0)
        elif part in ("super", "win", "cmd", "meta", "windows"):
            modifiers |= _X11_MOD_MASKS.get("super", 0)
        else:
            key_name = part
    return modifiers, key_name


# ─── X11 XGrabKey implementation ───

class _X11HotkeyManager:
    """Grab global hotkeys via X11 XGrabKey — most reliable on X11 sessions."""

    def __init__(self):
        self._callbacks: dict[tuple[int, int], Callable] = {}  # (mod_mask, keycode) -> callback
        self._display = None
        self._root = None
        self._thread: threading.Thread | None = None
        self._running = False

    def register(self, combo: str, action_name: str, callback: Callable):
        mod_mask, key_name = _parse_combo(combo)
        # We'll resolve keycodes in start() when display is open
        self._callbacks[(mod_mask, key_name)] = callback

    def start(self):
        self._display = xdisplay.Display()
        self._root = self._display.screen().root

        # Resolve key names to keycodes and set up grabs
        resolved = {}
        for (mod_mask, key_name), callback in self._callbacks.items():
            keysym = XK.string_to_keysym(key_name)
            if keysym == 0:
                # Try uppercase for single letters
                keysym = XK.string_to_keysym(key_name.upper())
            if keysym == 0:
                continue
            keycode = self._display.keysym_to_keycode(keysym)
            if keycode == 0:
                continue

            # Grab with all lock-mask combinations
            for lock_mask in _LOCK_MASKS:
                self._root.grab_key(
                    keycode,
                    mod_mask | lock_mask,
                    True,  # owner_events
                    X.GrabModeAsync,
                    X.GrabModeAsync,
                )

            resolved[(mod_mask, keycode)] = callback

        self._callbacks_resolved = resolved
        self._display.flush()

        self._running = True
        self._thread = threading.Thread(target=self._event_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._display:
            # Ungrab all keys
            for (mod_mask, keycode) in self._callbacks_resolved:
                for lock_mask in _LOCK_MASKS:
                    try:
                        self._root.ungrab_key(keycode, mod_mask | lock_mask)
                    except Exception:
                        pass
            try:
                self._display.flush()
                self._display.close()
            except Exception:
                pass
            self._display = None

    def _event_loop(self):
        """Listen for X11 KeyPress events on grabbed keys."""
        while self._running:
            try:
                # Check for pending events with a timeout
                if self._display.pending_events():
                    event = self._display.next_event()
                    if event.type == X.KeyPress:
                        # Strip lock masks to match our registered combos
                        clean_mask = event.state & ~(X.LockMask | X.Mod2Mask)
                        key = (clean_mask, event.detail)
                        cb = self._callbacks_resolved.get(key)
                        if cb:
                            threading.Thread(target=cb, daemon=True).start()
                else:
                    # No events pending, sleep briefly to avoid busy-wait
                    import time
                    time.sleep(0.05)
            except Exception:
                if self._running:
                    import time
                    time.sleep(0.1)


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
    """Unified hotkey manager — uses X11 XGrabKey if available, pynput otherwise."""

    def __init__(self):
        if _USE_XLIB:
            self._backend = _X11HotkeyManager()
            self._backend_name = "x11-grab"
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
