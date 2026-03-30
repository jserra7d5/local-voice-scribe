"""PyQt6 floating recording indicator and dictionary editor."""

import sys
import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QMetaObject, Q_ARG
from PyQt6.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PyQt6.QtWidgets import (
    QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QLabel,
)

from . import config as cfg


class StateSignal(QObject):
    """Thread-safe signal for state updates from daemon to overlay."""
    state_changed = pyqtSignal(str)
    show_editor = pyqtSignal()
    quit_signal = pyqtSignal()


class RecordingIndicator(QWidget):
    """Floating recording dot at bottom-center of screen."""

    COLORS = {
        "recording": QColor(255, 40, 40),
        "transcribing": QColor(255, 200, 0),
        "complete": QColor(40, 220, 40),
    }

    def __init__(self):
        super().__init__()
        self._state = "idle"
        self._pulse_alpha = 255
        self._pulse_direction = -1
        self._dot_size = 20

        # Frameless, always-on-top, translucent, click-through
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.setFixedSize(self._dot_size + 4, self._dot_size + 4)

        # Pulse animation timer
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_tick)

        # Auto-hide timer for complete state
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hide_timeout)

    def set_state(self, state: str):
        self._state = state
        if state == "recording":
            self._position_on_screen()
            self.show()
            self._pulse_alpha = 255
            self._pulse_timer.start(50)
            self._hide_timer.stop()
        elif state == "transcribing":
            self._pulse_timer.stop()
            self._pulse_alpha = 255
            self.update()
            self.show()
            self._hide_timer.stop()
        elif state == "complete":
            self._pulse_timer.stop()
            self._pulse_alpha = 255
            self.update()
            self.show()
            self._hide_timer.start(2000)
        else:
            self._pulse_timer.stop()
            self._hide_timer.stop()
            self.hide()

    def _position_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.x() + (geo.width() - self.width()) // 2
            y = geo.y() + geo.height() - self.height() - 40
            self.move(x, y)

    def _pulse_tick(self):
        self._pulse_alpha += self._pulse_direction * 15
        if self._pulse_alpha <= 80:
            self._pulse_alpha = 80
            self._pulse_direction = 1
        elif self._pulse_alpha >= 255:
            self._pulse_alpha = 255
            self._pulse_direction = -1
        self.update()

    def _on_hide_timeout(self):
        self.hide()

    def paintEvent(self, event):
        if self._state not in self.COLORS:
            return
        color = QColor(self.COLORS[self._state])
        color.setAlpha(self._pulse_alpha)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawEllipse(2, 2, self._dot_size, self._dot_size)
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
    """Manages the Qt application, recording indicator, and dictionary editor."""

    def __init__(self, daemon):
        self._daemon = daemon
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)

        self._signals = StateSignal()
        self._indicator = RecordingIndicator()
        self._editor = DictionaryEditor()

        self._signals.state_changed.connect(self._indicator.set_state)
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
