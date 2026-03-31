"""PyQt6 screen border overlay and settings window."""

from __future__ import annotations

import sys
import threading

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QPainter, QRegion
from PyQt6.QtWidgets import QApplication, QWidget

from . import config as cfg
from .settings import SettingsWindow

BORDER_THICKNESS = 6

def _build_border_colors(config: dict) -> dict[str, QColor]:
    return {
        "recording": QColor(config.get("border_color_recording", cfg.DEFAULTS["border_color_recording"])),
        "transcribing": QColor(config.get("border_color_transcribing", cfg.DEFAULTS["border_color_transcribing"])),
        "complete": QColor(config.get("border_color_complete", cfg.DEFAULTS["border_color_complete"])),
    }


class StateSignal(QObject):
    """Thread-safe signals for daemon-to-Qt updates."""

    state_changed = pyqtSignal(str)
    show_settings = pyqtSignal()
    quit_signal = pyqtSignal()
    clipboard_copy = pyqtSignal(str, object)


class BorderOverlay(QWidget):
    """Full-screen border effect on the active monitor."""

    def __init__(self, config: dict):
        super().__init__()
        self._state = "idle"
        self._alpha = 230
        self._color = QColor(0, 0, 0, 0)
        self._fade_step = 0
        self._enabled = True
        self._colors = _build_border_colors(config)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self._fade_tick)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._clear)
        self.update_config(config)

    def update_config(self, config: dict):
        self._enabled = bool(config.get("border_flash_enabled", True))
        self._colors = _build_border_colors(config)
        if not self._enabled:
            self._clear()

    def set_state(self, state: str):
        self._state = state
        self._fade_timer.stop()
        if not self._enabled:
            self._clear()
            return

        if state == "recording":
            self._show_border(self._colors["recording"], persistent=True)
        elif state == "transcribing":
            self._show_border(self._colors["transcribing"], persistent=False)
        elif state == "complete":
            self._show_border(self._colors["complete"], persistent=False)
        else:
            self._clear()

    def _show_border(self, color: QColor, persistent: bool):
        self._hide_timer.stop()
        self._fade_timer.stop()

        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if not screen:
            return

        geo = screen.geometry()
        self.setGeometry(geo)

        t = BORDER_THICKNESS
        outer = QRegion(0, 0, geo.width(), geo.height())
        inner = QRegion(t, t, geo.width() - 2 * t, geo.height() - 2 * t)
        self.setMask(outer.subtracted(inner))

        self._color = QColor(color)
        self._alpha = 230
        self._color.setAlpha(self._alpha)
        self.update()
        self.show()

        if not persistent:
            self._fade_step = 0
            self._fade_timer.start(50)

    def _fade_tick(self):
        self._fade_step += 1
        total_steps = 11
        if self._fade_step >= total_steps:
            self._fade_timer.stop()
            if self._state == "complete":
                self._hide_timer.start(200)
            else:
                self._clear()
            return
        self._alpha = int(230 * (1 - self._fade_step / total_steps))
        self._color.setAlpha(max(0, self._alpha))
        self.update()

    def _clear(self):
        self._fade_timer.stop()
        self._hide_timer.stop()
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), self._color)
        painter.end()


class OverlayApp:
    """Manages the Qt application, border overlay, settings window, and clipboard bridge."""

    def __init__(self, daemon):
        self._daemon = daemon
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)

        self._signals = StateSignal()
        self._border = BorderOverlay(daemon.config)
        self._settings = SettingsWindow(daemon)

        self._signals.state_changed.connect(self._on_state_changed)
        self._signals.show_settings.connect(self._settings.open_window)
        self._signals.quit_signal.connect(self._app.quit)
        self._signals.clipboard_copy.connect(self._copy_to_clipboard)

    def _on_state_changed(self, state: str):
        self._border.set_state(state)
        self._settings.set_state(state)

    def _copy_to_clipboard(self, text: str, response: object):
        clipboard = self._app.clipboard()
        clipboard.setText(text)
        self._app.processEvents()
        ok = clipboard.text() == text
        if isinstance(response, dict):
            response["ok"] = ok
            event = response.get("event")
            if isinstance(event, threading.Event):
                event.set()

    def set_state(self, state: str):
        self._signals.state_changed.emit(state)

    def update_config(self, config: dict):
        self._border.update_config(config)

    def show_settings_window(self):
        self._signals.show_settings.emit()

    def copy_to_clipboard(self, text: str, timeout_s: float = 1.0) -> bool:
        response = {"ok": False, "event": threading.Event()}
        self._signals.clipboard_copy.emit(text, response)
        response["event"].wait(timeout_s)
        return bool(response["ok"])

    def quit(self):
        self._signals.quit_signal.emit()

    def run(self):
        self._app.exec()
