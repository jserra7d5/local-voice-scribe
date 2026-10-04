"""PyQt6 screen border overlay and settings window."""

from __future__ import annotations

import sys

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
            | Qt.WindowType.WindowTransparentForInput
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
        # Wayland window masks control input, not visual clipping. Paint the
        # border itself and clear the backing buffer so fades do not accumulate.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        t = BORDER_THICKNESS
        outer = QRegion(self.rect())
        inner = QRegion(self.rect().adjusted(t, t, -t, -t))
        painter.setClipRegion(outer.subtracted(inner))
        painter.fillRect(self.rect(), self._color)
        painter.end()


class OverlayApp:
    """Manages the Qt application, border overlay, and settings window."""

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

    def _on_state_changed(self, state: str):
        self._border.set_state(state)
        self._settings.set_state(state)

    def set_state(self, state: str):
        self._signals.state_changed.emit(state)

    def update_config(self, config: dict):
        self._border.update_config(config)

    def show_settings_window(self):
        self._signals.show_settings.emit()

    def quit(self):
        self._signals.quit_signal.emit()

    def run(self):
        self._app.exec()
