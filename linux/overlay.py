"""PyQt6 screen border overlay and settings window."""

from __future__ import annotations

import math
import sys
from typing import Callable

from PyQt6.QtCore import QObject, QPointF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QScreen, QGradient, QLinearGradient, QPainter, QPen, QPolygonF, QRegion
from PyQt6.QtDBus import QDBusInterface, QDBusMessage
from PyQt6.QtWidgets import QApplication, QWidget

from . import config as cfg
from .settings import SettingsWindow

ANIM_TICK_MS = 33
# Bloom (complete): a thick soft glow on all four edges that pulses twice, then fades,
# so "ready to paste" is hard to miss. Intensity is linear between keyframes.
BLOOM_KEYS = ((0.0, 0.0), (0.1, 1.0), (0.3, 0.2), (0.45, 1.0), (0.8, 0.5), (1.4, 0.0))  # (s, intensity)
BLOOM_DEPTH = 34           # px the glow reaches inward
BLOOM_CORE = 6             # px of solid colour at the edge
# Shimmer (transcribing): waves of light flow left to right along the top and bottom
# edges, drawn as a repeating gradient shifted each tick.
SHIMMER_PERIOD = 900       # px between light crests
SHIMMER_SPEED = 24         # px per tick
SHIMMER_BANDS = ((26, 130), (6, 255))  # (height px, crest alpha): faint glow, then bright core
SHIMMER_FLOOR = 0.15       # alpha between crests, as a fraction of the crest

# Ripple: a neon line on each edge, a standing wave whose axis is the screen edge
# itself, pinned at the corners. Lobes swing inward; the outward half lies flat on the edge.
# It replaces the static border while recording; in silence it lies flat on the edges.
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


def _active_screen() -> QScreen | None:
    """The screen KWin calls active (it follows the mouse), else a guess from the cursor.

    Under XWayland, Qt sees the pointer only while it is over an X11 window, so
    QCursor.pos() is often stale and lands on the wrong monitor.
    """
    reply = QDBusInterface("org.kde.KWin", "/KWin", "org.kde.KWin").call("activeOutputName")
    name = reply.arguments()[0] if reply.type() == QDBusMessage.MessageType.ReplyMessage else None
    for screen in QApplication.screens():
        if screen.name() == name:
            return screen
    return QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()


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
        self._color = QColor(0, 0, 0, 0)
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

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._anim_tick)
        self.update_config(config)

    def update_config(self, config: dict):
        self._enabled = bool(config.get("border_flash_enabled", True))
        self._colors = _build_border_colors(config)
        if not self._enabled:
            self._clear()

    def set_state(self, state: str):
        self._state = state
        self._stop_anim()
        if not self._enabled or state not in self._colors:
            self._clear()
            return
        self._show(self._colors[state])
        self._anim_timer.start(ANIM_TICK_MS)

    def _show(self, color: QColor):
        screen = _active_screen()
        if not screen:
            return
        self.setGeometry(screen.geometry())
        self._color = QColor(color)
        self.update()
        self.show()

    def _bloom_intensity(self) -> float:
        elapsed = self._tick * ANIM_TICK_MS / 1000
        for (t0, v0), (t1, v1) in zip(BLOOM_KEYS, BLOOM_KEYS[1:]):
            if elapsed <= t1:
                return v0 + (v1 - v0) * (elapsed - t0) / (t1 - t0)
        return 0.0

    def _anim_tick(self):
        if self._state == "recording":
            self._envelope = max(self._level_fn(), self._envelope * RIPPLE_RELEASE)
        self._tick += 1
        if self._state == "complete" and self._tick * ANIM_TICK_MS / 1000 > BLOOM_KEYS[-1][0]:
            self._clear()
            return
        self.update(self._anim_region())

    def _stop_anim(self):
        self._anim_timer.stop()
        self._envelope = 0.0
        self._tick = 0

    def _anim_region(self) -> QRegion:
        if self._state == "transcribing":
            h = SHIMMER_BANDS[0][0]
            return QRegion(0, 0, self.width(), h) | QRegion(0, self.height() - h, self.width(), h)
        band = BLOOM_DEPTH if self._state == "complete" else RIPPLE_MAX_DEPTH + RIPPLE_GLOW[0][0]
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
        self._stop_anim()
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Clear the backing buffer so animation frames do not accumulate.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self._state == "recording":
            self._paint_neon(painter)
        elif self._state == "transcribing":
            self._paint_shimmer(painter)
        elif self._state == "complete":
            self._paint_bloom(painter)
        painter.end()

    def _paint_bloom(self, painter: QPainter):
        intensity = self._bloom_intensity()
        if intensity <= 0:
            return
        w, h, d = self.width(), self.height(), BLOOM_DEPTH
        edge = QColor(self._color)
        edge.setAlpha(int(255 * intensity))
        clear = QColor(self._color)
        clear.setAlpha(0)
        # (glow rect, gradient from the edge inward)
        for rect, (x0, y0, x1, y1) in (
            ((0, 0, w, d), (0, 0, 0, d)),
            ((0, h - d, w, d), (0, h, 0, h - d)),
            ((0, 0, d, h), (0, 0, d, 0)),
            ((w - d, 0, d, h), (w, 0, w - d, 0)),
        ):
            gradient = QLinearGradient(x0, y0, x1, y1)
            gradient.setColorAt(0.0, edge)
            gradient.setColorAt(BLOOM_CORE / d, edge)
            gradient.setColorAt(1.0, clear)
            painter.fillRect(*rect, gradient)

    def _paint_shimmer(self, painter: QPainter):
        offset = self._tick * SHIMMER_SPEED
        for height, crest in SHIMMER_BANDS:
            gradient = QLinearGradient(offset, 0, offset + SHIMMER_PERIOD, 0)
            gradient.setSpread(QGradient.Spread.RepeatSpread)
            for stop, strength in ((0.0, SHIMMER_FLOOR), (0.35, SHIMMER_FLOOR), (0.5, 1.0), (0.65, SHIMMER_FLOOR), (1.0, SHIMMER_FLOOR)):
                c = QColor(self._color)
                c.setAlpha(int(crest * strength))
                gradient.setColorAt(stop, c)
            painter.fillRect(0, 0, self.width(), height, gradient)
            painter.fillRect(0, self.height() - height, self.width(), height, gradient)

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
        # Python runs signal handlers only between bytecodes; an idle Qt loop runs
        # none, so SIGTERM would wait until systemd's SIGKILL. Wake Python regularly.
        wake = QTimer()
        wake.timeout.connect(lambda: None)
        wake.start(250)
        self._app.exec()
