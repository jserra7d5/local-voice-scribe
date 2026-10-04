"""PyQt6 screen border overlay and settings window."""

from __future__ import annotations

import math
import sys
from typing import Callable

from PyQt6.QtCore import QObject, QPointF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QPainter, QPen, QPolygonF, QRegion
from PyQt6.QtWidgets import QApplication, QWidget

from . import config as cfg
from .settings import SettingsWindow

BORDER_THICKNESS = 6
# Ripple: a neon line on each edge, a standing wave whose axis is the screen edge
# itself, pinned at the corners. Lobes swing inward; the outward half lies flat on the edge.
# It replaces the static border while recording; in silence it lies flat on the edges.
RIPPLE_TICK_MS = 33
RIPPLE_MAX_DEPTH = 40      # px the line swings inward at full level
RIPPLE_HALF_WAVE = 800     # px per antinode; each edge fits a whole number of them
RIPPLE_OMEGA = (0.32, 0.55)  # radians per tick for the two mixed modes
RIPPLE_EDGE_PHASE = 1.7    # phase offset between edges, so they move out of step
RIPPLE_SAMPLE = 24         # px between line points
RIPPLE_RELEASE = 0.85      # per-tick decay, so peaks linger briefly instead of flickering
# Neon: wide faint strokes for the glow, then a bright thin core.
# Both take the recording colour, so the line stays the configured red.
RIPPLE_GLOW = ((16, 70), (7, 160))  # (width px, alpha)
RIPPLE_CORE_WIDTH = 3

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

    def __init__(self, config: dict, level_fn: Callable[[], float] = lambda: 0.0):
        super().__init__()
        self._level_fn = level_fn
        self._envelope = 0.0
        self._tick = 0
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

        self._ripple_timer = QTimer(self)
        self._ripple_timer.timeout.connect(self._ripple_tick)
        self.update_config(config)

    def update_config(self, config: dict):
        self._enabled = bool(config.get("border_flash_enabled", True))
        self._colors = _build_border_colors(config)
        if not self._enabled:
            self._clear()

    def set_state(self, state: str):
        self._state = state
        self._fade_timer.stop()
        self._stop_ripple()
        if not self._enabled:
            self._clear()
            return

        if state == "recording":
            self._show_border(self._colors["recording"], persistent=True)
            self._ripple_timer.start(RIPPLE_TICK_MS)
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

    def _ripple_tick(self):
        self._envelope = max(self._level_fn(), self._envelope * RIPPLE_RELEASE)
        self._tick += 1
        self.update(self._ripple_region())

    def _stop_ripple(self):
        self._ripple_timer.stop()
        self._envelope = 0.0

    def _ripple_region(self) -> QRegion:
        band = BORDER_THICKNESS + RIPPLE_MAX_DEPTH + RIPPLE_GLOW[0][0]
        return QRegion(self.rect()).subtracted(QRegion(self.rect().adjusted(band, band, -band, -band)))

    def _neon_lines(self) -> list[QPolygonF]:
        """One line per edge: a standing wave about the screen edge, corners as nodes.

        Depth inward = level * MAX * max(mix, 0), where mix is two standing modes in [-1, 1].
        """
        x0, y0, x1, y1 = 0, 0, self.width(), self.height()
        amp = RIPPLE_MAX_DEPTH * self._envelope
        # (start, end, inward normal) for each edge, clockwise from the top-left corner
        edges = [
            ((x0, y0), (x1, y0), (0, 1)),
            ((x1, y0), (x1, y1), (-1, 0)),
            ((x1, y1), (x0, y1), (0, -1)),
            ((x0, y1), (x0, y0), (1, 0)),
        ]
        lines = []
        for e, ((ax, ay), (bx, by), (nx, ny)) in enumerate(edges):
            length = abs(bx - ax) + abs(by - ay)
            n = max(1, round(length / RIPPLE_HALF_WAVE))
            phase = e * RIPPLE_EDGE_PHASE
            a = math.sin(RIPPLE_OMEGA[0] * self._tick + phase)
            b = math.sin(RIPPLE_OMEGA[1] * self._tick + 2 * phase)
            steps = max(1, int(length / RIPPLE_SAMPLE))
            points = []
            for i in range(steps + 1):
                f = i / steps
                mix = 0.65 * a * math.sin(math.pi * n * f) + 0.35 * b * math.sin(math.pi * (2 * n + 1) * f)
                d = amp * max(mix, 0.0)
                points.append(QPointF(ax + (bx - ax) * f + nx * d, ay + (by - ay) * f + ny * d))
            lines.append(QPolygonF(points))
        # Separate per-edge lines: one screen-sized polygon makes the rasteriser scan the whole screen.
        return lines

    def _clear(self):
        self._stop_ripple()
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
        if self._state == "recording":
            self._paint_neon(painter)
        else:
            t = BORDER_THICKNESS
            outer = QRegion(self.rect())
            inner = QRegion(self.rect().adjusted(t, t, -t, -t))
            painter.setClipRegion(outer.subtracted(inner))
            painter.fillRect(self.rect(), self._color)
        painter.end()

    def _paint_neon(self, painter: QPainter):
        lines = self._neon_lines()
        # Only the thin core is antialiased: AA on the wide faint glow costs ~10x
        # and does not show.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for width, alpha in RIPPLE_GLOW:
            glow = QColor(self._color)
            glow.setAlpha(alpha)
            painter.setPen(QPen(glow, width, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
            for line in lines:
                painter.drawPolyline(line)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        core = QColor(self._color)
        core.setAlpha(255)
        painter.setPen(QPen(core, RIPPLE_CORE_WIDTH))
        for line in lines:
            painter.drawPolyline(line)


class OverlayApp:
    """Manages the Qt application, border overlay, and settings window."""

    def __init__(self, daemon):
        self._daemon = daemon
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)

        self._signals = StateSignal()
        self._border = BorderOverlay(daemon.config, lambda: daemon.recorder.level)
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
