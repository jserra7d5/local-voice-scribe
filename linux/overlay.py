"""PyQt6 screen border overlay and dictionary editor."""

import sys

from PyQt6.QtCore import Qt, QTimer, QRect, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QPainter, QBrush, QCursor, QFont, QRegion
from PyQt6.QtWidgets import (
    QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QLabel,
)

from . import config as cfg

BORDER_THICKNESS = 6

BORDER_COLORS = {
    "recording": QColor(255, 40, 40),
    "transcribing": QColor(255, 200, 0),
    "complete": QColor(40, 220, 40),
}


class StateSignal(QObject):
    """Thread-safe signal for state updates from daemon to overlay."""
    state_changed = pyqtSignal(str)
    show_editor = pyqtSignal()
    quit_signal = pyqtSignal()


class BorderOverlay(QWidget):
    """Full-screen border effect on the active monitor, matching Hammerspoon's hs.canvas borders."""

    def __init__(self):
        super().__init__()
        self._state = "idle"
        self._alpha = 230
        self._color = QColor(0, 0, 0, 0)
        self._fade_step = 0
        self._fade_gen = 0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # Fade-out timer for flash effects
        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self._fade_tick)

        # Auto-hide timer for complete state
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_complete_done)

    def set_state(self, state: str):
        self._state = state
        self._fade_timer.stop()

        if state == "recording":
            self._show_border(BORDER_COLORS["recording"], persistent=True)
        elif state == "transcribing":
            self._show_border(BORDER_COLORS["transcribing"], persistent=False)
        elif state == "complete":
            self._show_border(BORDER_COLORS["complete"], persistent=False)
        else:
            self._clear()

    def _show_border(self, color: QColor, persistent: bool):
        """Position on active screen, set up the border mask, and show."""
        self._hide_timer.stop()
        self._fade_timer.stop()

        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if not screen:
            return

        geo = screen.geometry()
        self.setGeometry(geo)

        # Build a mask that is only the border strips (hollow rectangle)
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
            # Fade out over ~550ms
            self._fade_gen += 1
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

    def _on_complete_done(self):
        self._clear()

    def _clear(self):
        self._fade_timer.stop()
        self._hide_timer.stop()
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), self._color)
        painter.end()


class DictionaryEditor(QDialog):
    """Floating dictionary editor dialog with dark theme."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Whisper Dictionary")
        self.setMinimumSize(400, 300)
        self.resize(450, 350)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog
        )

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QLabel("One word per line, or: wrong -> right")
        header.setStyleSheet("color: #888; font-size: 13px;")
        layout.addWidget(header)

        self.text_edit = QTextEdit()
        self.text_edit.setFont(QFont("Monospace", 12))
        layout.addWidget(self.text_edit)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("saveBtn")
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog { background: #1e1e1e; }
            QTextEdit {
                background: #2d2d2d; color: #d4d4d4;
                border: 1px solid #444; border-radius: 4px;
                padding: 8px; selection-background-color: #264f78;
            }
            QTextEdit:focus { border-color: #666; }
            QPushButton {
                padding: 6px 16px; border: none; border-radius: 4px;
                font-size: 13px; background: #444; color: #ccc;
            }
            QPushButton:hover { background: #555; }
            QPushButton#saveBtn { background: #2ea043; color: white; }
            QPushButton#saveBtn:hover { background: #3fb950; }
        """)

    def open_editor(self):
        """Load dictionary content and show the dialog."""
        content = ""
        if cfg.DICTIONARY_FILE.exists():
            content = cfg.DICTIONARY_FILE.read_text()
        self.text_edit.setPlainText(content)
        self.show()
        self.raise_()
        self.activateWindow()
        self.text_edit.setFocus()

    def _save(self):
        content = self.text_edit.toPlainText()
        try:
            cfg.DICTIONARY_FILE.write_text(content)
            count = sum(1 for line in content.splitlines() if line.strip())
            from .notifications import notify
            notify(f"Dictionary saved ({count} entries)")
        except OSError as e:
            from .notifications import notify
            notify(f"Save failed: {e}", title="Error")
        self.accept()


class OverlayApp:
    """Manages the Qt application, border overlay, and dictionary editor."""

    def __init__(self, daemon):
        self._daemon = daemon
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)

        self._signals = StateSignal()
        self._border = BorderOverlay()
        self._editor = DictionaryEditor()

        self._signals.state_changed.connect(self._border.set_state)
        self._signals.show_editor.connect(self._editor.open_editor)
        self._signals.quit_signal.connect(self._app.quit)

    def set_state(self, state: str):
        """Thread-safe state update (called from daemon thread)."""
        self._signals.state_changed.emit(state)

    def show_dictionary_editor(self):
        """Thread-safe editor open (called from daemon thread)."""
        self._signals.show_editor.emit()

    def quit(self):
        """Thread-safe quit (called from signal handler)."""
        self._signals.quit_signal.emit()

    def run(self):
        """Run the Qt event loop (blocks)."""
        self._app.exec()
