"""PyQt settings and dictionary dialogs for Linux."""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QColorDialog,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from . import config as cfg
from .ducking import list_active_streams
from .notifications import notify
from .recorder import list_input_sources


def _normalize_hotkey(value: str) -> str:
    parts = []
    final_key = None
    for raw_part in value.lower().replace("<", "").replace(">", "").split("+"):
        part = raw_part.strip()
        if not part:
            continue
        if part in {"ctrl", "control"}:
            token = "ctrl"
        elif part == "shift":
            token = "shift"
        elif part in {"alt", "option"}:
            token = "alt"
        elif part in {"super", "win", "cmd", "meta", "windows"}:
            token = "super"
        else:
            token = part

        if token in {"ctrl", "shift", "alt", "super"}:
            if token not in parts:
                parts.append(token)
        else:
            if final_key is not None:
                raise ValueError("Hotkeys must contain exactly one non-modifier key")
            final_key = token

    if final_key is None:
        raise ValueError("Hotkeys must include a key")
    if not parts:
        raise ValueError("Hotkeys must include at least one modifier")
    parts.append(final_key)
    return "+".join(parts)


def _normalize_color_hex(value: str, default: str) -> str:
    text = value.strip().lower()
    if QColor(text).isValid() and len(text) == 7 and text.startswith("#"):
        return text
    raise ValueError(f"Colors must be #RRGGBB hex values, for example {default}")


def _summarize_active_apps(streams: list[dict]) -> list[dict]:
    """Collapse active playback streams into one row per application binary."""
    apps: dict[str, dict] = {}
    for stream in streams:
        binary = stream.get("binary") or "unknown"
        app = apps.setdefault(binary, {
            "binary": binary,
            "display_name": stream.get("display_name") or binary,
            "stream_count": 0,
            "average_percent_total": 0,
            "media_names": [],
        })
        app["stream_count"] += 1
        app["average_percent_total"] += int(stream.get("average_percent", 100))
        media_name = (stream.get("media_name") or "").strip()
        if media_name and media_name not in app["media_names"]:
            app["media_names"].append(media_name)

    summarized = []
    for app in apps.values():
        count = app["stream_count"]
        summarized.append({
            "binary": app["binary"],
            "display_name": app["display_name"],
            "stream_count": count,
            "average_percent": round(app["average_percent_total"] / count) if count else 100,
            "media_preview": "\n".join(app["media_names"]) if app["media_names"] else "",
        })

    summarized.sort(key=lambda item: (item["display_name"].lower(), item["binary"]))
    return summarized


class DictionaryEditor(QDialog):
    """Floating dictionary editor dialog with dark theme."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Voice Dictionary")
        self.setMinimumSize(520, 360)
        self.resize(620, 420)
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog)

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        header = QLabel("Bias words: one per line. Replacements: `wrong -> right`.")
        header.setStyleSheet("color: #8ea2b6; font-size: 13px;")
        layout.addWidget(header)

        self.text_edit = QTextEdit()
        self.text_edit.setFont(QFont("Monospace", 12))
        layout.addWidget(self.text_edit)

        button_row = QHBoxLayout()
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save)
        button_row.addWidget(save_btn)

        layout.addLayout(button_row)

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog { background: #10151d; color: #d7e0ea; }
            QTextEdit {
                background: #17202b;
                color: #d7e0ea;
                border: 1px solid #314154;
                border-radius: 8px;
                padding: 10px;
                selection-background-color: #2d5a88;
            }
            QPushButton {
                padding: 7px 14px;
                border: 1px solid #314154;
                border-radius: 7px;
                background: #192330;
                color: #d7e0ea;
            }
            QPushButton:hover { background: #223041; }
            QPushButton#primaryBtn {
                background: #2d7d46;
                border-color: #2d7d46;
                color: white;
            }
            QPushButton#primaryBtn:hover { background: #399956; }
        """)

    def open_editor(self):
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
            notify(f"Dictionary saved ({count} entries)")
            self.accept()
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))


class RuleDialog(QDialog):
    """Create a new per-process ducking rule."""

    def __init__(self, active_apps: list[dict], parent=None):
        super().__init__(parent)
        self._active_apps = active_apps
        self.result_rule: dict | None = None

        self.setWindowTitle("Add Ducking Rule")
        self.setModal(True)
        self.resize(460, 220)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.app_combo = QComboBox()
        self.app_combo.addItem("Manual rule", None)
        for app in active_apps:
            label = f"{app['display_name']} ({app['binary'] or 'unknown'})"
            if app.get("stream_count", 0) > 1:
                label += f"  -  {app['stream_count']} active streams"
            self.app_combo.addItem(label, app)
        self.app_combo.currentIndexChanged.connect(self._sync_from_selected_app)
        form.addRow("Running app", self.app_combo)

        self.display_name_edit = QLineEdit()
        form.addRow("Display name", self.display_name_edit)

        self.match_binary_edit = QLineEdit()
        self.match_binary_edit.setPlaceholderText("firefox")
        form.addRow("Match binary", self.match_binary_edit)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Custom level", "custom")
        self.mode_combo.addItem("Bypass ducking", "bypass")
        self.mode_combo.currentIndexChanged.connect(self._sync_mode)
        form.addRow("Rule mode", self.mode_combo)

        self.level_spin = QSpinBox()
        self.level_spin.setRange(0, 100)
        self.level_spin.setSuffix("%")
        self.level_spin.setValue(25)
        form.addRow("Duck level", self.level_spin)

        layout.addLayout(form)

        button_row = QHBoxLayout()
        button_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)
        add_btn = QPushButton("Add Rule")
        add_btn.setObjectName("primaryBtn")
        add_btn.clicked.connect(self._accept)
        button_row.addWidget(add_btn)
        layout.addLayout(button_row)

        self._sync_from_selected_app()
        self._sync_mode()

    def _sync_from_selected_app(self):
        app = self.app_combo.currentData()
        if not app:
            return
        self.display_name_edit.setText(app["display_name"])
        self.match_binary_edit.setText(app["binary"])

    def _sync_mode(self):
        self.level_spin.setEnabled(self.mode_combo.currentData() != "bypass")

    def _accept(self):
        match_binary = self.match_binary_edit.text().strip().lower()
        display_name = self.display_name_edit.text().strip() or match_binary
        if not match_binary:
            QMessageBox.warning(self, "Missing match key", "Choose a running process or enter a match binary.")
            return
        self.result_rule = {
            "match_binary": match_binary,
            "display_name": display_name or match_binary,
            "mode": self.mode_combo.currentData(),
            "duck_level": self.level_spin.value(),
        }
        self.accept()


class RecordingHistoryDialog(QDialog):
    """Browse archived recordings and rerun transcription from saved audio."""

    def __init__(self, daemon, parent=None):
        super().__init__(parent)
        self._daemon = daemon
        self._entries: list[dict] = []

        self.setWindowTitle("Recording History")
        self.setMinimumSize(860, 520)
        self.resize(980, 620)
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        hint = QLabel("Keeps the last 10 recordings with raw audio, transcript, timestamp, and duration.")
        hint.setStyleSheet("color: #8ea2b6; font-size: 13px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Recorded", "Length", "Status", "Transcript"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._sync_detail)
        layout.addWidget(self.table, 1)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("Select a recording to inspect the saved transcript and metadata.")
        self.detail.setMinimumHeight(180)
        layout.addWidget(self.detail)

        button_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        button_row.addWidget(refresh_btn)

        rerun_btn = QPushButton("Rerun Transcription")
        rerun_btn.setObjectName("primaryBtn")
        rerun_btn.clicked.connect(self._rerun_selected)
        button_row.addWidget(rerun_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_row.addStretch()
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog { background: #10151d; color: #d7e0ea; }
            QTextEdit, QTableWidget {
                background: #17202b;
                color: #d7e0ea;
                border: 1px solid #314154;
                border-radius: 8px;
            }
            QHeaderView::section {
                background: #182331;
                color: #9cb0c4;
                padding: 6px;
                border: none;
            }
            QPushButton {
                padding: 7px 14px;
                border: 1px solid #314154;
                border-radius: 7px;
                background: #192330;
                color: #d7e0ea;
            }
            QPushButton:hover { background: #223041; }
            QPushButton#primaryBtn {
                background: #1f6f4a;
                border-color: #1f6f4a;
                color: white;
            }
            QPushButton#primaryBtn:hover { background: #27885a; }
        """)

    def open_dialog(self):
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def refresh(self):
        self._entries = self._daemon.list_recording_history()
        self.table.setRowCount(0)
        for entry in self._entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("started_at", 0)))
            duration = f"{int(entry.get('duration_s', 0))}s"
            status = entry.get("status", "archived")
            preview = entry.get("transcript_preview", "") or entry.get("error", "") or "Raw audio only"
            self.table.setItem(row, 0, QTableWidgetItem(stamp))
            self.table.setItem(row, 1, QTableWidgetItem(duration))
            self.table.setItem(row, 2, QTableWidgetItem(status))
            self.table.setItem(row, 3, QTableWidgetItem(preview))
        if self.table.rowCount():
            self.table.selectRow(0)
        else:
            self.detail.setPlainText("No archived recordings yet.")

    def _selected_entry(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._entries):
            return None
        return self._entries[row]

    def _sync_detail(self):
        entry = self._selected_entry()
        if not entry:
            self.detail.clear()
            return
        lines = [
            f"Recorded: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry.get('started_at', 0)))}",
            f"Length: {int(entry.get('duration_s', 0))}s",
            f"Status: {entry.get('status', 'archived')}",
            f"Audio: {entry.get('audio_path', '')}",
        ]
        transcript_path = entry.get("transcript_path", "")
        if transcript_path:
            lines.append(f"Transcript: {transcript_path}")
        error = entry.get("error", "")
        if error:
            lines.append(f"Error: {error}")
        lines.append("")

        transcript_text = ""
        if transcript_path:
            try:
                transcript_text = Path(transcript_path).read_text()
            except OSError:
                transcript_text = ""
        if transcript_text:
            lines.append(transcript_text)
        else:
            lines.append("No saved transcript yet for this recording.")
        self.detail.setPlainText("\n".join(lines))

    def _rerun_selected(self):
        entry = self._selected_entry()
        if not entry:
            QMessageBox.information(self, "No selection", "Choose a recording first.")
            return
        try:
            self._daemon.rerun_transcription_from_history(entry["id"])
        except Exception as exc:
            QMessageBox.critical(self, "Rerun failed", str(exc))
            return
        notify("Reran transcription from archived audio")
        self.refresh()


class SettingsWindow(QDialog):
    """Main Linux settings window opened by the global hotkey."""

    def __init__(self, daemon, parent=None):
        super().__init__(parent)
        self._daemon = daemon
        self._dictionary_editor = DictionaryEditor(self)
        self._history_dialog = RecordingHistoryDialog(daemon, self)
        self._active_apps: list[dict] = []

        self.setWindowTitle("Local Voice Scribe Settings")
        self.setMinimumSize(880, 760)
        self.resize(980, 860)
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog)

        self._setup_ui()
        self._apply_style()
        self._apply_control_sizing()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("Local Voice Scribe")
        title.setStyleSheet("font-size: 24px; font-weight: 600; color: #f4f7fb;")
        layout.addWidget(title)

        subtitle = QLabel("Recording, ducking, hotkeys, and dictionary controls for the Linux daemon.")
        subtitle.setStyleSheet("font-size: 13px; color: #8ea2b6;")
        layout.addWidget(subtitle)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self.tabs.addTab(self._wrap_tab(self._build_general_tab()), "General")
        self.tabs.addTab(self._wrap_tab(self._build_ducking_tab()), "Ducking")

        button_row = QHBoxLayout()
        self.status_label = QLabel("Daemon state: idle")
        self.status_label.setStyleSheet("color: #8ea2b6;")
        button_row.addWidget(self.status_label)
        button_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save Settings")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save)
        button_row.addWidget(save_btn)
        layout.addLayout(button_row)

    def _wrap_tab(self, content: QWidget) -> QScrollArea:
        """Keep tab layouts stable by scrolling instead of vertical collapse."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        scroll.setWidget(content)
        return scroll

    def _build_color_control(self) -> tuple[QLineEdit, QPushButton]:
        edit = QLineEdit()
        edit.setPlaceholderText("#46aaff")
        edit.setMaxLength(7)
        button = QPushButton("Pick")
        button.setMinimumWidth(74)
        return edit, button

    def _build_color_row(self, edit: QLineEdit, button: QPushButton) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        row_layout.addWidget(edit, 1)
        row_layout.addWidget(button)
        return row

    def _pick_color(self, target: QLineEdit):
        initial = QColor(target.text().strip() or target.placeholderText())
        color = QColorDialog.getColor(initial, self, "Choose Border Flash Color")
        if color.isValid():
            target.setText(color.name())

    def _build_general_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(14)

        general_box = QGroupBox("Daemon")
        general_form = QFormLayout(general_box)
        self.idle_timeout_spin = QSpinBox()
        self.idle_timeout_spin.setRange(30, 3600)
        self.idle_timeout_spin.setSuffix(" sec")
        general_form.addRow("Server idle timeout", self.idle_timeout_spin)
        layout.addWidget(general_box)

        recording_box = QGroupBox("Input Source")
        recording_form = QFormLayout(recording_box)
        self.audio_device_combo = QComboBox()
        recording_form.addRow("Microphone", self.audio_device_combo)
        layout.addWidget(recording_box)

        hotkey_box = QGroupBox("Global Hotkeys")
        hotkey_layout = QVBoxLayout(hotkey_box)
        hotkey_layout.setContentsMargins(14, 18, 14, 14)
        hotkey_layout.setSpacing(10)

        hotkey_form = QFormLayout()
        hotkey_form.setContentsMargins(0, 0, 0, 0)
        hotkey_form.setVerticalSpacing(12)
        hotkey_form.setHorizontalSpacing(16)
        self.record_hotkey_edit = QLineEdit()
        self.record_hotkey_edit.setPlaceholderText("super+alt+r")
        hotkey_form.addRow("Toggle recording", self.record_hotkey_edit)

        self.settings_hotkey_edit = QLineEdit()
        self.settings_hotkey_edit.setPlaceholderText("super+alt+c")
        hotkey_form.addRow("Open settings", self.settings_hotkey_edit)

        self.transcripts_hotkey_edit = QLineEdit()
        self.transcripts_hotkey_edit.setPlaceholderText("super+alt+t")
        hotkey_form.addRow("Open transcripts", self.transcripts_hotkey_edit)
        hotkey_layout.addLayout(hotkey_form)

        hint = QLabel("Use forms like `super+alt+r`, `ctrl+shift+s`, or `alt+f8`.")
        hint.setStyleSheet("color: #8ea2b6; font-size: 12px;")
        hint.setWordWrap(True)
        hotkey_layout.addWidget(hint)
        hotkey_box.setMinimumHeight(220)
        layout.addWidget(hotkey_box)

        border_box = QGroupBox("Border Flash")
        border_layout = QVBoxLayout(border_box)
        border_layout.setContentsMargins(14, 18, 14, 14)
        border_layout.setSpacing(10)

        self.border_flash_enabled_checkbox = QCheckBox("Enable border flash status overlay")
        border_layout.addWidget(self.border_flash_enabled_checkbox)

        border_form = QFormLayout()
        border_form.setContentsMargins(0, 0, 0, 0)
        border_form.setVerticalSpacing(12)
        border_form.setHorizontalSpacing(16)

        self.recording_color_edit, recording_color_pick = self._build_color_control()
        recording_color_pick.clicked.connect(lambda: self._pick_color(self.recording_color_edit))
        border_form.addRow("Recording start", self._build_color_row(self.recording_color_edit, recording_color_pick))

        self.transcribing_color_edit, transcribing_color_pick = self._build_color_control()
        transcribing_color_pick.clicked.connect(lambda: self._pick_color(self.transcribing_color_edit))
        border_form.addRow("Recording stop", self._build_color_row(self.transcribing_color_edit, transcribing_color_pick))

        self.complete_color_edit, complete_color_pick = self._build_color_control()
        complete_color_pick.clicked.connect(lambda: self._pick_color(self.complete_color_edit))
        border_form.addRow("Transcription complete", self._build_color_row(self.complete_color_edit, complete_color_pick))

        border_layout.addLayout(border_form)
        layout.addWidget(border_box)

        tools_box = QGroupBox("Tools")
        tools_layout = QHBoxLayout(tools_box)
        tools_layout.setContentsMargins(14, 18, 14, 14)
        tools_layout.setSpacing(10)
        dict_btn = QPushButton("Open Dictionary")
        dict_btn.clicked.connect(self._dictionary_editor.open_editor)
        tools_layout.addWidget(dict_btn)

        history_btn = QPushButton("Recording History")
        history_btn.clicked.connect(self._history_dialog.open_dialog)
        tools_layout.addWidget(history_btn)

        transcripts_btn = QPushButton("Open Transcript Folder")
        transcripts_btn.clicked.connect(self._daemon.open_transcript_folder)
        tools_layout.addWidget(transcripts_btn)

        restart_btn = QPushButton("Restart Daemon")
        restart_btn.clicked.connect(self._restart_daemon)
        tools_layout.addWidget(restart_btn)
        tools_layout.addStretch()
        tools_box.setMinimumHeight(92)
        layout.addWidget(tools_box)

        layout.addStretch()
        return widget

    def _build_ducking_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 0, 0, 0)

        controls_box = QGroupBox("Default Ducking")
        controls_layout = QVBoxLayout(controls_box)
        controls_layout.setContentsMargins(14, 18, 14, 14)
        controls_layout.setSpacing(10)

        controls_form = QFormLayout()
        controls_form.setContentsMargins(0, 0, 0, 0)
        controls_form.setVerticalSpacing(12)
        controls_form.setHorizontalSpacing(16)

        self.duck_enabled_checkbox = QCheckBox("Enable playback ducking while recording")
        controls_form.addRow("", self.duck_enabled_checkbox)

        self.duck_level_spin = QSpinBox()
        self.duck_level_spin.setRange(0, 100)
        self.duck_level_spin.setSuffix("%")
        controls_form.addRow("Default duck level", self.duck_level_spin)

        self.duck_ramp_down_spin = QDoubleSpinBox()
        self.duck_ramp_down_spin.setRange(0.0, 5.0)
        self.duck_ramp_down_spin.setSingleStep(0.1)
        self.duck_ramp_down_spin.setSuffix(" sec")
        controls_form.addRow("Ramp down", self.duck_ramp_down_spin)

        self.duck_ramp_up_spin = QDoubleSpinBox()
        self.duck_ramp_up_spin.setRange(0.0, 5.0)
        self.duck_ramp_up_spin.setSingleStep(0.1)
        self.duck_ramp_up_spin.setSuffix(" sec")
        controls_form.addRow("Ramp up", self.duck_ramp_up_spin)
        controls_layout.addLayout(controls_form)
        controls_box.setMinimumHeight(0)
        controls_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(controls_box)

        streams_box = QGroupBox("Detected Playback Apps")
        streams_layout = QVBoxLayout(streams_box)
        streams_layout.setContentsMargins(14, 18, 14, 14)
        streams_layout.setSpacing(10)
        streams_hint = QLabel("Apps are grouped by process binary, so multiple Firefox tabs show up as one Firefox row.")
        streams_hint.setStyleSheet("color: #8ea2b6; font-size: 12px;")
        streams_hint.setWordWrap(True)
        streams_layout.addWidget(streams_hint)

        self.active_apps_table = QTableWidget(0, 4)
        self.active_apps_table.setHorizontalHeaderLabels(["App", "Binary", "Streams", "Now Playing"])
        self.active_apps_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.active_apps_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.active_apps_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.active_apps_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.active_apps_table.verticalHeader().setVisible(False)
        self.active_apps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.active_apps_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.active_apps_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.active_apps_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.active_apps_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.active_apps_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.active_apps_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.active_apps_table.setSizeAdjustPolicy(QAbstractItemView.SizeAdjustPolicy.AdjustToContents)
        self.active_apps_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.active_apps_table.setWordWrap(True)
        self.active_apps_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        streams_layout.addWidget(self.active_apps_table)

        stream_buttons_widget = QWidget()
        stream_buttons_widget.setMinimumHeight(52)
        stream_buttons = QHBoxLayout(stream_buttons_widget)
        stream_buttons.setContentsMargins(0, 0, 0, 0)
        stream_buttons.setSpacing(10)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_active_streams)
        stream_buttons.addWidget(refresh_btn)
        add_rule_btn = QPushButton("Add Rule For Selected App")
        add_rule_btn.clicked.connect(self._add_rule_from_selected_app)
        stream_buttons.addWidget(add_rule_btn)
        stream_buttons.addStretch()
        streams_layout.addWidget(stream_buttons_widget)
        streams_box.setMinimumHeight(0)
        streams_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout.addWidget(streams_box, 1)

        rules_box = QGroupBox("Per-App Overrides")
        rules_layout = QVBoxLayout(rules_box)
        rules_layout.setContentsMargins(14, 18, 14, 14)
        rules_layout.setSpacing(10)
        self.rules_table = QTableWidget(0, 4)
        self.rules_table.setHorizontalHeaderLabels(["Display Name", "Match Binary", "Mode", "Duck Level"])
        self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rules_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.rules_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.rules_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.rules_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rules_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rules_table.setSizeAdjustPolicy(QAbstractItemView.SizeAdjustPolicy.AdjustToContents)
        self.rules_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        rules_layout.addWidget(self.rules_table)

        rules_buttons = QHBoxLayout()
        remove_btn = QPushButton("Remove Selected Rule")
        remove_btn.clicked.connect(self._remove_selected_rule)
        rules_buttons.addWidget(remove_btn)
        rules_buttons.addStretch()
        rules_layout.addLayout(rules_buttons)
        rules_box.setMinimumHeight(0)
        rules_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout.addWidget(rules_box, 1)

        return widget

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog {
                background: #0f1722;
                color: #d7e0ea;
            }
            QTabWidget::pane {
                border: 1px solid #243244;
                background: #121c28;
                border-radius: 12px;
                padding: 10px;
            }
            QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: #121c28;
                width: 12px;
                margin: 6px 0 6px 0;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: #334960;
                min-height: 28px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover {
                background: #496683;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QTabBar::tab {
                background: #172331;
                color: #9cb0c4;
                padding: 8px 14px;
                margin-right: 6px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }
            QTabBar::tab:selected {
                background: #223246;
                color: #f4f7fb;
            }
            QGroupBox {
                border: 1px solid #243244;
                border-radius: 10px;
                margin-top: 10px;
                padding-top: 14px;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
            }
            QPushButton {
                min-height: 22px;
                padding: 7px 14px;
                border: 1px solid #314154;
                border-radius: 7px;
                background: #192330;
                color: #d7e0ea;
            }
            QPushButton:hover { background: #223041; }
            QPushButton#primaryBtn {
                background: #1f6f4a;
                border-color: #1f6f4a;
                color: white;
            }
            QPushButton#primaryBtn:hover { background: #27885a; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget {
                background: #17202b;
                color: #d7e0ea;
                border: 1px solid #314154;
                border-radius: 7px;
                min-height: 24px;
                padding: 6px 10px;
            }
            QComboBox::drop-down {
                width: 28px;
                border: none;
            }
            QSpinBox::up-button, QSpinBox::down-button,
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
                width: 22px;
            }
            QTableWidget {
                padding: 0px;
            }
            QHeaderView::section {
                background: #182331;
                color: #9cb0c4;
                padding: 6px;
                border: none;
            }
        """)

    def _apply_control_sizing(self):
        controls = [
            self.idle_timeout_spin,
            self.audio_device_combo,
            self.record_hotkey_edit,
            self.settings_hotkey_edit,
            self.transcripts_hotkey_edit,
            self.recording_color_edit,
            self.transcribing_color_edit,
            self.complete_color_edit,
            self.duck_level_spin,
            self.duck_ramp_down_spin,
            self.duck_ramp_up_spin,
        ]
        for control in controls:
            control.setMinimumHeight(38)

        self.active_apps_table.verticalHeader().setDefaultSectionSize(36)
        self.rules_table.verticalHeader().setDefaultSectionSize(36)
        self.active_apps_table.setMinimumHeight(88)
        self.rules_table.setMinimumHeight(88)

    def _fit_table_height(self, table: QTableWidget, *, min_rows: int) -> None:
        header_height = table.horizontalHeader().height()
        frame_height = table.frameWidth() * 2
        visible_rows = max(table.rowCount(), min_rows)
        rows_height = sum(table.rowHeight(row) for row in range(table.rowCount()))
        if table.rowCount() < min_rows:
            default_row_height = table.verticalHeader().defaultSectionSize()
            rows_height += (min_rows - table.rowCount()) * default_row_height
        target_height = header_height + frame_height + rows_height + 4
        table.setMinimumHeight(target_height)
        table.setMaximumHeight(target_height)

    def open_window(self):
        self._load_config()
        self._refresh_active_streams()
        self.show()
        self.raise_()
        self.activateWindow()

    def set_state(self, state: str):
        self.status_label.setText(f"Daemon state: {state}")

    def _load_config(self):
        config = dict(self._daemon.config)
        self.idle_timeout_spin.setValue(int(config.get("server_idle_timeout", 300)))
        self.record_hotkey_edit.setText(config.get("hotkey_toggle_recording", "super+alt+r"))
        self.settings_hotkey_edit.setText(config.get("hotkey_open_settings", "super+alt+c"))
        self.transcripts_hotkey_edit.setText(config.get("hotkey_open_transcripts", "super+alt+t"))
        self.border_flash_enabled_checkbox.setChecked(bool(config.get("border_flash_enabled", True)))
        self.recording_color_edit.setText(config.get("border_color_recording", cfg.DEFAULTS["border_color_recording"]))
        self.transcribing_color_edit.setText(
            config.get("border_color_transcribing", cfg.DEFAULTS["border_color_transcribing"])
        )
        self.complete_color_edit.setText(config.get("border_color_complete", cfg.DEFAULTS["border_color_complete"]))

        self.duck_enabled_checkbox.setChecked(bool(config.get("duck_enabled", False)))
        self.duck_level_spin.setValue(int(config.get("duck_level", 10)))
        self.duck_ramp_down_spin.setValue(float(config.get("duck_ramp_down", 0.5)))
        self.duck_ramp_up_spin.setValue(float(config.get("duck_ramp_up", 1.0)))

        self._populate_audio_devices(config.get("audio_device"))
        self._populate_rules(config.get("duck_rules", []))
        self.set_state(self._daemon.state)

    def _populate_audio_devices(self, selected_device):
        self.audio_device_combo.clear()
        self.audio_device_combo.addItem("Auto detect / default input", None)
        for source in list_input_sources():
            label = source["label"]
            if source["is_focusrite"]:
                label = f"{label}  (Focusrite)"
            self.audio_device_combo.addItem(label, source["name"])

        index = 0
        if selected_device:
            for i in range(self.audio_device_combo.count()):
                if self.audio_device_combo.itemData(i) == selected_device:
                    index = i
                    break
        self.audio_device_combo.setCurrentIndex(index)

    def _refresh_active_streams(self):
        self._active_apps = _summarize_active_apps(list_active_streams())
        self.active_apps_table.setRowCount(0)
        for app in self._active_apps:
            row = self.active_apps_table.rowCount()
            self.active_apps_table.insertRow(row)
            self.active_apps_table.setItem(row, 0, QTableWidgetItem(app["display_name"]))
            self.active_apps_table.setItem(row, 1, QTableWidgetItem(app["binary"]))
            self.active_apps_table.setItem(row, 2, QTableWidgetItem(str(app["stream_count"])))
            preview = app["media_preview"] or "Active audio"
            self.active_apps_table.setItem(row, 3, QTableWidgetItem(preview))
        self.active_apps_table.resizeRowsToContents()
        self.active_apps_table.resizeColumnsToContents()
        self._fit_table_height(self.active_apps_table, min_rows=2)

    def _populate_rules(self, rules: list[dict]):
        self.rules_table.setRowCount(0)
        for rule in rules:
            row = self.rules_table.rowCount()
            self.rules_table.insertRow(row)

            name_item = QTableWidgetItem(rule.get("display_name", rule.get("match_binary", "")))
            binary_item = QTableWidgetItem(rule.get("match_binary", ""))
            name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsEditable)
            binary_item.setFlags(binary_item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.rules_table.setItem(row, 0, name_item)
            self.rules_table.setItem(row, 1, binary_item)

            mode_combo = QComboBox()
            mode_combo.addItem("Custom level", "custom")
            mode_combo.addItem("Bypass ducking", "bypass")
            mode_combo.setCurrentIndex(1 if rule.get("mode") == "bypass" else 0)
            self.rules_table.setCellWidget(row, 2, mode_combo)

            level_spin = QSpinBox()
            level_spin.setRange(0, 100)
            level_spin.setSuffix("%")
            level_spin.setValue(int(rule.get("duck_level", self.duck_level_spin.value())))
            level_spin.setEnabled(mode_combo.currentData() != "bypass")
            mode_combo.currentIndexChanged.connect(
                lambda _idx, spin=level_spin, combo=mode_combo: spin.setEnabled(combo.currentData() != "bypass")
            )
            self.rules_table.setCellWidget(row, 3, level_spin)
        self.rules_table.resizeRowsToContents()
        self._fit_table_height(self.rules_table, min_rows=2)

    def _add_rule_from_selected_app(self):
        selected_row = self.active_apps_table.currentRow()
        active_apps = list(self._active_apps)
        if selected_row >= 0 and selected_row < len(active_apps):
            selected_app = active_apps[selected_row]
            active_apps = [selected_app] + [app for i, app in enumerate(active_apps) if i != selected_row]

        dialog = RuleDialog(active_apps, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.result_rule:
            return
        row = self.rules_table.rowCount()
        self.rules_table.insertRow(row)
        self.rules_table.setItem(row, 0, QTableWidgetItem(dialog.result_rule["display_name"]))
        self.rules_table.setItem(row, 1, QTableWidgetItem(dialog.result_rule["match_binary"]))

        mode_combo = QComboBox()
        mode_combo.addItem("Custom level", "custom")
        mode_combo.addItem("Bypass ducking", "bypass")
        mode_combo.setCurrentIndex(1 if dialog.result_rule["mode"] == "bypass" else 0)
        self.rules_table.setCellWidget(row, 2, mode_combo)

        level_spin = QSpinBox()
        level_spin.setRange(0, 100)
        level_spin.setSuffix("%")
        level_spin.setValue(int(dialog.result_rule["duck_level"]))
        level_spin.setEnabled(mode_combo.currentData() != "bypass")
        mode_combo.currentIndexChanged.connect(
            lambda _idx, spin=level_spin, combo=mode_combo: spin.setEnabled(combo.currentData() != "bypass")
        )
        self.rules_table.setCellWidget(row, 3, level_spin)
        self.rules_table.resizeRowsToContents()
        self._fit_table_height(self.rules_table, min_rows=2)

    def _remove_selected_rule(self):
        row = self.rules_table.currentRow()
        if row >= 0:
            self.rules_table.removeRow(row)
            self.rules_table.resizeRowsToContents()
            self._fit_table_height(self.rules_table, min_rows=2)

    def _collect_rules(self) -> list[dict]:
        rules = []
        for row in range(self.rules_table.rowCount()):
            display_name_item = self.rules_table.item(row, 0)
            match_binary_item = self.rules_table.item(row, 1)
            if not display_name_item or not match_binary_item:
                continue

            match_binary = match_binary_item.text().strip().lower()
            if not match_binary:
                continue

            mode_combo = self.rules_table.cellWidget(row, 2)
            level_spin = self.rules_table.cellWidget(row, 3)
            mode = mode_combo.currentData() if mode_combo else "custom"
            level = level_spin.value() if level_spin else self.duck_level_spin.value()
            rules.append({
                "display_name": display_name_item.text().strip() or match_binary,
                "match_binary": match_binary,
                "mode": mode,
                "duck_level": level,
            })
        return rules

    def _save(self):
        try:
            settings = {
                "server_idle_timeout": int(self.idle_timeout_spin.value()),
                "audio_device": self.audio_device_combo.currentData(),
                "hotkey_toggle_recording": _normalize_hotkey(self.record_hotkey_edit.text()),
                "hotkey_open_settings": _normalize_hotkey(self.settings_hotkey_edit.text()),
                "hotkey_open_transcripts": _normalize_hotkey(self.transcripts_hotkey_edit.text()),
                "border_flash_enabled": self.border_flash_enabled_checkbox.isChecked(),
                "border_color_recording": _normalize_color_hex(
                    self.recording_color_edit.text(), cfg.DEFAULTS["border_color_recording"]
                ),
                "border_color_transcribing": _normalize_color_hex(
                    self.transcribing_color_edit.text(), cfg.DEFAULTS["border_color_transcribing"]
                ),
                "border_color_complete": _normalize_color_hex(
                    self.complete_color_edit.text(), cfg.DEFAULTS["border_color_complete"]
                ),
                "duck_enabled": self.duck_enabled_checkbox.isChecked(),
                "duck_level": int(self.duck_level_spin.value()),
                "duck_ramp_down": float(self.duck_ramp_down_spin.value()),
                "duck_ramp_up": float(self.duck_ramp_up_spin.value()),
                "duck_rules": self._collect_rules(),
            }
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid settings", str(exc))
            return

        try:
            self._daemon.save_settings(settings)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return

        notify("Settings saved")
        self.accept()

    def _restart_daemon(self):
        answer = QMessageBox.question(
            self,
            "Restart Local Voice Scribe",
            "Restart the daemon now? The window and hotkeys will disappear briefly while it relaunches.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            self._daemon.restart_via_helper()
        except Exception as exc:
            QMessageBox.critical(self, "Restart failed", str(exc))
