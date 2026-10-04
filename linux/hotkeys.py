"""Global hotkey manager.

Wayland: the XDG GlobalShortcuts portal, so the compositor owns and consumes the keys.
X11: XGrabKey via python-xlib. Last resort: pynput.
"""

import os
import secrets
import threading
from typing import Callable

APP_ID = "local-voice-scribe"  # must match the installed .desktop file

_USE_PORTAL = False
if os.environ.get("WAYLAND_DISPLAY"):
    try:
        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
        from jeepney.io.blocking import Proxy, open_dbus_connection
        _USE_PORTAL = True
    except ImportError:
        pass

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

    def register(self, combo: str, action_name: str, callback: Callable, description: str = ""):
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


# ─── XDG GlobalShortcuts portal (Wayland) ───

_PORTAL = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop",
                      interface="org.freedesktop.portal.GlobalShortcuts") if _USE_PORTAL else None
_PORTAL_MODS = {"ctrl": "CTRL", "control": "CTRL", "shift": "SHIFT", "alt": "ALT", "option": "ALT",
                "super": "LOGO", "win": "LOGO", "cmd": "LOGO", "meta": "LOGO", "windows": "LOGO"}


def _portal_trigger(combo: str) -> str:
    """'super+alt+r' -> 'LOGO+ALT+r', the portal's preferred_trigger format (xkb key names)."""
    parts = [p.strip() for p in combo.lower().replace("<", "").replace(">", "").split("+") if p.strip()]
    # Function keys are the one common xkb name that is not lowercase ("F9").
    return "+".join(_PORTAL_MODS.get(p) or (p.upper() if p[0] == "f" and p[1:].isdigit() else p) for p in parts)


class _PortalHotkeyManager:
    """Global shortcuts through org.freedesktop.portal.GlobalShortcuts.

    The compositor consumes the keys and lists them in its shortcut settings. A
    combo is only a preferred trigger: it applies on first bind (KDE asks the user
    to confirm once), and after that the user's assignment in System Settings wins.
    """

    def __init__(self, log=lambda msg: None):
        self._log = log
        self._shortcuts: dict[str, tuple[str, str, Callable]] = {}  # id -> (combo, description, callback)
        self.bound: dict[str, str] = {}  # id -> trigger description, once bound
        self._running = False
        self._thread: threading.Thread | None = None

    def register(self, combo: str, action_name: str, callback: Callable, description: str = ""):
        self._shortcuts[action_name] = (combo, description or action_name, callback)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _request(self, conn, method: str, signature: str, args: tuple, options: dict) -> dict:
        """Call a portal method that answers through a Request object; return its results."""
        token = "lvs" + secrets.token_hex(6)
        path = f"/org/freedesktop/portal/desktop/request/{conn.unique_name[1:].replace('.', '_')}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=path)
        Proxy(message_bus, conn).AddMatch(rule)
        with conn.filter(rule) as queue:
            options = {**options, "handle_token": ("s", token)}
            conn.send_and_get_reply(new_method_call(_PORTAL, method, signature, (*args, options)))
            code, results = conn.recv_until_filtered(queue).body  # waits for any user dialog
        if code != 0:
            raise RuntimeError(f"{method} answered {code}")
        return {k: v for k, (_sig, v) in results.items()}

    def _run(self):
        try:
            conn = open_dbus_connection(bus="SESSION")
        except Exception as e:
            self._log(f"hotkeys: no session bus: {e}")
            return
        with conn:
            try:
                registry = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop",
                                       interface="org.freedesktop.host.portal.Registry")
                conn.send_and_get_reply(new_method_call(registry, "Register", "sa{sv}", (APP_ID, {})))
                session = self._request(conn, "CreateSession", "a{sv}", (),
                                        {"session_handle_token": ("s", "lvs" + secrets.token_hex(6))})["session_handle"]
                shortcuts = [(sid, {"description": ("s", desc), "preferred_trigger": ("s", _portal_trigger(combo))})
                             for sid, (combo, desc, _cb) in self._shortcuts.items()]
                bound = self._request(conn, "BindShortcuts", "oa(sa{sv})sa{sv}", (session, shortcuts, ""), {})
                self.bound = {sid: props.get("trigger_description", ("s", ""))[1] for sid, props in bound["shortcuts"]}
                self._log(f"hotkeys: portal bound {self.bound}")
            except Exception as e:
                self._log(f"hotkeys: portal bind failed: {e}")
                return

            rule = MatchRule(type="signal", interface="org.freedesktop.portal.GlobalShortcuts",
                             member="Activated", path="/org/freedesktop/portal/desktop")
            Proxy(message_bus, conn).AddMatch(rule)
            with conn.filter(rule) as queue:
                while self._running:
                    try:
                        msg = conn.recv_until_filtered(queue, timeout=0.5)
                    except TimeoutError:
                        continue
                    msg_session, sid = msg.body[0], msg.body[1]
                    entry = self._shortcuts.get(sid)
                    if msg_session == session and entry:
                        threading.Thread(target=entry[2], daemon=True).start()
            close = new_method_call(DBusAddress(session, bus_name="org.freedesktop.portal.Desktop",
                                                interface="org.freedesktop.portal.Session"), "Close")
            try:
                conn.send_and_get_reply(close, timeout=2)
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

    def register(self, combo: str, action_name: str, callback: Callable, description: str = ""):
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
    """Unified hotkey manager — portal on Wayland, X11 XGrabKey on X11, pynput otherwise."""

    def __init__(self, log=lambda msg: None):
        if _USE_PORTAL:
            self._backend = _PortalHotkeyManager(log)
            self._backend_name = "portal"
        elif _USE_XLIB:
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

    @property
    def system_managed(self) -> bool:
        """True when the compositor owns key assignment, so in-app combos are only defaults."""
        return self._backend_name == "portal"

    @property
    def bound(self) -> dict[str, str]:
        """Shortcut id -> the trigger the compositor actually assigned (portal only)."""
        return getattr(self._backend, "bound", {})

    def register(self, action_id: str, combo: str, description: str, callback: Callable):
        if self._backend:
            self._backend.register(combo, action_id, callback, description)

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
