"""Main daemon: state machine, recording loop, and orchestration."""

import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as cfg
from .clipboard import copy_to_clipboard
from .dictionary import apply_replacements
from .ducking import DuckingController
from . import history
from .hotkeys import HotkeyManager, format_hotkey
from .notifications import notify
from .recorder import Recorder
from .server import WhisperServer
from .transcriber import transcribe


def _normalized_repetition_key(chunk: str) -> str:
    return re.sub(r"\s+", " ", chunk).strip().lower()


def _split_repetition_chunks(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    if parts:
        return parts
    return [text.strip()] if text.strip() else []


def _find_triplicate_repetition(text: str) -> str | None:
    chunks = _split_repetition_chunks(text)
    repeat_count = 1
    previous_key = None
    previous_chunk = ""

    for chunk in chunks:
        key = _normalized_repetition_key(chunk)
        if len(key) < 24 or len(key.split()) < 4:
            previous_key = key
            previous_chunk = chunk
            repeat_count = 1
            continue
        if key == previous_key:
            repeat_count += 1
            if repeat_count >= 3:
                return previous_chunk
        else:
            repeat_count = 1
        previous_key = key
        previous_chunk = chunk
    return None


class Daemon:
    """Local Voice Scribe daemon for Linux."""

    def __init__(self):
        self.config = cfg.load_config()
        self._state = "idle"
        self._session_id = 0
        self._recording_started_at: float | None = None
        self._last_duration: float | None = None
        self._transcription_started_for: int | None = None
        self._history_entry_id: str | None = None
        self._idle_reset_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()

        # Clear log
        cfg.LOG_FILE.write_text(f"=== daemon start {time.strftime('%c')} ===\n")

        self.server = WhisperServer(self.config, self.log)
        self.recorder = Recorder(self.config, self.log)
        self.ducking = DuckingController(self.config, self.log)
        self.hotkeys = HotkeyManager()

        # Overlay (lazy import — only loaded if PyQt6 available)
        self._overlay = None
        self._overlay_module = None

    def log(self, msg: str):
        try:
            with open(cfg.LOG_FILE, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except OSError:
            pass

    def update_state(self, state: str):
        self._state = state
        try:
            cfg.STATE_FILE.write_text(state)
        except OSError:
            pass
        # Update overlay if available
        if self._overlay:
            try:
                self._overlay.set_state(state)
            except Exception:
                pass

    @property
    def state(self) -> str:
        return self._state

    def run(self):
        """Main entry point. Blocks until shutdown."""
        self.log("daemon starting")
        self.update_state("idle")

        # Signal handlers for clean shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Try to initialize overlay
        self._init_overlay()

        # Pre-warm whisper server
        self.server.launch_if_needed()

        # Register hotkeys
        self._register_hotkeys()

        rec_display = format_hotkey(self.config.get("hotkey_toggle_recording", "super+alt+r"))
        settings_display = format_hotkey(self.config.get("hotkey_open_settings", "super+alt+c"))
        trans_display = format_hotkey(self.config.get("hotkey_open_transcripts", "super+alt+t"))
        notify(
            f"Recording: {rec_display}\n"
            f"Settings: {settings_display}\n"
            f"Transcripts: {trans_display}",
            title="Local Voice Scribe ready",
        )
        self.log(f"hotkey backend: {self.hotkeys.backend_name}")
        self.log(f"hotkeys: record={rec_display}, settings={settings_display}, transcripts={trans_display}")

        # If overlay is running, it has its own event loop (Qt)
        if self._overlay:
            self.log("running with overlay (Qt event loop)")
            self._overlay.run()  # blocks until quit
        else:
            # No overlay — just wait for shutdown
            self.log("running without overlay (waiting for shutdown)")
            self._shutdown_event.wait()

        self._cleanup()

    def toggle_recording(self):
        """Toggle between recording and idle states."""
        with self._lock:
            if self._state in ("idle", "complete"):
                if self._idle_reset_timer:
                    self._idle_reset_timer.cancel()
                    self._idle_reset_timer = None
                self._start_recording()
            elif self._state == "recording":
                self._stop_recording()
            else:
                self.log(f"ignoring toggle, state={self._state}")

    def _start_recording(self):
        self._session_id += 1
        self._transcription_started_for = None
        self._last_duration = None
        self._history_entry_id = None
        gen = self._session_id

        self.update_state("recording")
        self.log(f"startRecording session={gen}")
        self._recording_started_at = time.time()

        # Keep server alive during active work
        self.server.suspend_idle_timer()
        self.server.launch_if_needed()
        self.server.suspend_idle_timer()  # launch arms it, suspend again

        gen = self._session_id

        if not self.recorder.start(on_crash_callback=lambda: self._on_ffmpeg_crash(gen)):
            notify("Recording failed (could not start ffmpeg)", title="Error")
            self.update_state("idle")
            self.server.reset_idle_timer()
            return

        self.ducking.begin_session()
        notify("Recording started")

    def _stop_recording(self):
        self.log("stopRecording")
        gen = self._session_id

        if self._recording_started_at:
            elapsed = time.time() - self._recording_started_at
            self._last_duration = max(1, round(elapsed))
        else:
            self._last_duration = None

        self.ducking.end_session()
        self.update_state("transcribing")
        notify("Recording stopped. Transcribing...")

        def on_ffmpeg_exit():
            self._archive_recording_audio()
            self._start_transcription(gen)

        self.recorder.stop(on_exit_callback=on_ffmpeg_exit)

    def _start_transcription(self, gen: int):
        """Run transcription after recording stops."""
        if gen != self._session_id:
            self.log("transcription: stale session, ignoring")
            return
        if self._transcription_started_for == gen:
            return
        self._transcription_started_for = gen

        try:
            self._do_transcription(gen)
        except Exception as e:
            self.log(f"doTranscription ERROR: {e}")
            self._finish_transcription(f"Transcription error: {e}")

    def _do_transcription(self, gen: int):
        if gen != self._session_id:
            return

        # Check file size
        if not cfg.TEMP_AUDIO_FILE.exists() or cfg.TEMP_AUDIO_FILE.stat().st_size < 1000:
            self._finish_transcription("Recording too short")
            return

        # Wait for server
        if not self.server.is_up():
            self.log("server not up, launching and waiting...")
            self.server.launch_if_needed()
            self.server.suspend_idle_timer()
            if not self.server.wait_for_ready():
                self._finish_transcription("Whisper server failed to start")
                return

        text = self._transcribe_with_repetition_retry(cfg.TEMP_AUDIO_FILE, history_entry_id=self._history_entry_id)

        if gen != self._session_id:
            self.log("transcription callback: stale session")
            return

        if not text:
            self._finish_transcription("No transcription found")
            return

        # Apply replacements
        text = apply_replacements(text)
        if self._history_entry_id:
            history.update_recording(self._history_entry_id, transcript_text=text, status="transcribed", error="")

        # Archive
        self._archive_transcription(text)

        # Clipboard
        if self._copy_transcription_to_clipboard(text):
            self.log("copied to clipboard")
        else:
            self.log("clipboard copy failed")
            notify("Transcription finished, but clipboard copy failed", title="Clipboard error", timeout_ms=5000)
            self.update_state("idle")
            self._recording_started_at = None
            self._last_duration = None
            self._transcription_started_for = None
            self._history_entry_id = None
            if cfg.TEMP_AUDIO_FILE.exists():
                cfg.TEMP_AUDIO_FILE.unlink()
            self.server.reset_idle_timer()
            return

        # Show result
        preview = text[:60] if len(text) > 60 else text
        self.update_state("complete")
        notify(f"Copied to clipboard\n\n{preview}", title="Transcription complete", timeout_ms=5000)

        self._recording_started_at = None
        self._last_duration = None
        self._transcription_started_for = None
        self._history_entry_id = None

        # Reset to idle after 3 seconds
        self._idle_reset_timer = threading.Timer(3.0, lambda: self._idle_reset(gen))
        self._idle_reset_timer.daemon = True
        self._idle_reset_timer.start()

        # Clean up temp file
        if cfg.TEMP_AUDIO_FILE.exists():
            cfg.TEMP_AUDIO_FILE.unlink()

        self.server.reset_idle_timer()

    def _idle_reset(self, gen: int):
        if gen == self._session_id:
            self.update_state("idle")

    def _on_ffmpeg_crash(self, gen: int):
        """Handle unexpected ffmpeg exit while still in recording state."""
        if gen != self._session_id:
            return
        if self._state == "recording":
            self.log("ffmpeg crashed unexpectedly during recording")
            self._finish_transcription("Recording failed (ffmpeg crashed)")

    def _finish_transcription(self, message: str):
        """Clean up after a failed or empty transcription."""
        self.ducking.end_session()
        if self._history_entry_id:
            history.update_recording(self._history_entry_id, status="failed", error=message or "")
        if cfg.TEMP_AUDIO_FILE.exists():
            cfg.TEMP_AUDIO_FILE.unlink()
        self.update_state("idle")
        self.server.reset_idle_timer()
        self._transcription_started_for = None
        self._recording_started_at = None
        self._last_duration = None
        self._history_entry_id = None
        if message:
            notify(message)
            self.log(f"finishTranscription: {message}")

    def _archive_recording_audio(self):
        """Persist a copy of the raw recording before temp cleanup."""
        if self._history_entry_id:
            return
        if not cfg.TEMP_AUDIO_FILE.exists() or cfg.TEMP_AUDIO_FILE.stat().st_size < 1000:
            return
        started_at = self._recording_started_at or time.time()
        duration = self._last_duration or 0
        try:
            entry = history.create_recording(cfg.TEMP_AUDIO_FILE, started_at=started_at, duration_s=duration)
        except Exception as exc:
            self.log(f"history archive failed: {exc}")
            return
        if entry:
            self._history_entry_id = entry["id"]
            self.log(f"archived raw recording to {entry['audio_path']}")

    def _archive_transcription(self, text: str):
        """Save transcription to the transcript archive directory."""
        cfg.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        started = self._recording_started_at or time.time()
        duration = self._last_duration or 0
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started))
        filename = f"transcript_{stamp}__dur-{int(duration)}s.txt"
        path = cfg.TRANSCRIPT_DIR / filename
        try:
            path.write_text(text)
            self.log(f"archived transcript to {path}")
        except OSError as e:
            self.log(f"archive failed: {e}")

    def open_transcript_folder(self):
        """Open the transcript folder in the file manager."""
        cfg.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(
                ["xdg-open", str(cfg.TRANSCRIPT_DIR)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            self.log(f"failed to open transcript folder: {e}")
            notify("Could not open transcript folder")

    def list_recording_history(self) -> list[dict]:
        return history.list_recordings()

    def rerun_transcription_from_history(self, entry_id: str) -> str:
        with self._lock:
            if self._state in {"recording", "transcribing"}:
                raise RuntimeError("Wait for the current recording/transcription to finish first.")

            entry = history.get_recording(entry_id)
            if not entry:
                raise FileNotFoundError("Recording history entry not found.")

            audio_path = Path(entry.get("audio_path", ""))
            if not audio_path.exists():
                raise FileNotFoundError(f"Archived audio is missing: {audio_path}")

            self.update_state("transcribing")
            self.server.suspend_idle_timer()

        try:
            if not self.server.is_up():
                self.server.launch_if_needed()
                self.server.suspend_idle_timer()
                if not self.server.wait_for_ready():
                    raise RuntimeError("Whisper server failed to start")

            text = self._transcribe_with_repetition_retry(audio_path, history_entry_id=entry_id, allow_retry=False)
            if not text:
                raise RuntimeError("No transcription found")

            text = apply_replacements(text)
            history.update_recording(entry_id, transcript_text=text, status="transcribed", error="")
            self._archive_transcription(text)

            if not self._copy_transcription_to_clipboard(text):
                raise RuntimeError("Clipboard copy failed")

            preview = text[:60] if len(text) > 60 else text
            self.update_state("complete")
            notify(f"Copied rerun transcription\n\n{preview}", title="Transcription complete", timeout_ms=5000)
            return text
        except Exception as exc:
            history.update_recording(entry_id, status="failed", error=str(exc))
            self.update_state("idle")
            raise
        finally:
            self.server.reset_idle_timer()

    def _transcribe_with_repetition_retry(
        self,
        audio_path: Path,
        *,
        history_entry_id: str | None,
        allow_retry: bool = True,
    ) -> str | None:
        self.log(f"sending file to server: {audio_path}")
        text = transcribe(self.log, audio_file=audio_path)
        repeated_chunk = _find_triplicate_repetition(text or "")
        if repeated_chunk and allow_retry:
            self.log(f"detected repeated transcription chunk, retrying once: [{repeated_chunk}]")
            if history_entry_id:
                history.update_recording(
                    history_entry_id,
                    status="retrying",
                    error="Detected repeated text pattern; rerunning transcription once.",
                )
            notify("Detected repeated transcription pattern. Rerunning once...", timeout_ms=2500)
            retry_text = transcribe(self.log, audio_file=audio_path)
            retry_repeated_chunk = _find_triplicate_repetition(retry_text or "")
            if retry_repeated_chunk:
                self.log(f"retry still shows repeated chunk: [{retry_repeated_chunk}]")
            return retry_text
        return text

    def _open_transcript_folder(self):
        self.open_transcript_folder()

    def _open_settings_window(self):
        """Open the PyQt settings dialog."""
        if self._overlay:
            try:
                self._overlay.show_settings_window()
            except Exception as e:
                self.log(f"settings window error: {e}")
                notify(f"Settings window error: {e}")
        else:
            try:
                subprocess.Popen(
                    ["xdg-open", str(cfg.USER_CONFIG_FILE)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                notify("Could not open settings file")

    def _init_overlay(self):
        """Try to initialize the PyQt6 overlay. Gracefully degrades if unavailable."""
        try:
            from .overlay import OverlayApp
            self._overlay = OverlayApp(self)
            self.log("overlay initialized")
        except ImportError as e:
            self.log(f"overlay unavailable (PyQt6 not installed): {e}")
            self._overlay = None
        except Exception as e:
            self.log(f"overlay init failed: {e}")
            self._overlay = None

    def _signal_handler(self, signum, frame):
        self.log(f"received signal {signum}")
        if self._overlay:
            try:
                self._overlay.quit()
            except Exception:
                pass
        self._shutdown_event.set()

    def _cleanup(self):
        self.log("daemon cleanup")
        self.hotkeys.stop()
        self.ducking.end_session()
        # Stop ffmpeg if still recording
        if self.recorder.is_recording:
            self.log("stopping ffmpeg on shutdown")
            self.recorder.stop()
        self.server.stop()
        self.update_state("idle")
        self.log("daemon stopped")

    def _register_hotkeys(self):
        self.hotkeys.stop()
        self.hotkeys = HotkeyManager()
        rec_key = self.config.get("hotkey_toggle_recording", "super+alt+r")
        settings_key = self.config.get("hotkey_open_settings", "super+alt+c")
        trans_key = self.config.get("hotkey_open_transcripts", "super+alt+t")
        self.hotkeys.register(rec_key, self.toggle_recording)
        self.hotkeys.register(settings_key, self._open_settings_window)
        self.hotkeys.register(trans_key, self._open_transcript_folder)
        self.hotkeys.start()

    def _copy_transcription_to_clipboard(self, text: str) -> bool:
        if self._overlay:
            try:
                if self._overlay.copy_to_clipboard(text):
                    return True
            except Exception as e:
                self.log(f"qt clipboard failed: {e}")
        return copy_to_clipboard(text)

    def save_settings(self, updates: dict):
        """Persist user settings and apply them to the running daemon."""
        with self._lock:
            new_config = dict(self.config)
            new_config.update(updates)
            cfg.save_user_config(new_config)
            self.config = cfg.load_config()
            self.server.config = self.config
            self.recorder.config = self.config
            self.ducking.update_config(self.config)
            if self._overlay:
                self._overlay.update_config(self.config)
            self._register_hotkeys()
            self.server.reset_idle_timer()

    def restart_via_helper(self):
        """Relaunch the daemon from an external helper, then exit this process."""
        repo_root = self.config.get("repo_root")
        if repo_root:
            repo_root = Path(repo_root)
        else:
            repo_root = Path(__file__).resolve().parent.parent

        launcher_path = self.config.get("launcher_path")
        if launcher_path:
            launcher = Path(launcher_path)
        else:
            launcher = cfg.CONFIG_DIR / "bin" / "local-voice-scribe-linux"
        if not launcher.exists():
            raise FileNotFoundError(f"Launcher not found: {launcher}")

        env_bits = []
        for key in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY"):
            value = os.environ.get(key)
            if value:
                env_bits.append(f"{key}={shlex.quote(value)}")

        helper_cmd = (
            "sleep 1; "
            f"{' '.join(env_bits)} "
            f"nohup {shlex.quote(str(launcher))} >/tmp/local-voice-scribe.out 2>&1 < /dev/null &"
        )
        self.log(f"restart helper cmd: {helper_cmd}")
        subprocess.Popen(
            ["/bin/sh", "-lc", helper_cmd],
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        notify("Restarting Local Voice Scribe…", timeout_ms=2000)
        if self._overlay:
            try:
                self._overlay.quit()
            except Exception:
                pass
        self._shutdown_event.set()
