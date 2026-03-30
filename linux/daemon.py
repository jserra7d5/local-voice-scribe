"""Main daemon: state machine, recording loop, and orchestration."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as cfg
from .clipboard import copy_to_clipboard
from .dictionary import apply_replacements
from .hotkeys import HotkeyManager, format_hotkey
from .notifications import notify
from .recorder import Recorder
from .server import WhisperServer
from .transcriber import transcribe


class Daemon:
    """Local Voice Scribe daemon for Linux."""

    def __init__(self):
        self.config = cfg.load_config()
        self._state = "idle"
        self._session_id = 0
        self._recording_started_at: float | None = None
        self._last_duration: float | None = None
        self._transcription_started_for: int | None = None
        self._idle_reset_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()

        # Clear log
        cfg.LOG_FILE.write_text(f"=== daemon start {time.strftime('%c')} ===\n")

        self.server = WhisperServer(self.config, self.log)
        self.recorder = Recorder(self.config, self.log)
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
        rec_key = self.config.get("hotkey_toggle_recording", "super+alt+r")
        dict_key = self.config.get("hotkey_dictionary_editor", "super+alt+c")
        trans_key = self.config.get("hotkey_open_transcripts", "super+alt+t")

        self.hotkeys.register(rec_key, self.toggle_recording)
        self.hotkeys.register(dict_key, self._open_dictionary_editor)
        self.hotkeys.register(trans_key, self._open_transcript_folder)
        self.hotkeys.start()

        rec_display = format_hotkey(rec_key)
        dict_display = format_hotkey(dict_key)
        trans_display = format_hotkey(trans_key)
        notify(
            f"Recording: {rec_display}\n"
            f"Dictionary: {dict_display}\n"
            f"Transcripts: {trans_display}",
            title="Local Voice Scribe ready",
        )
        self.log(f"hotkeys: record={rec_display}, dict={dict_display}, transcripts={trans_display}")

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

        notify("Recording started")

    def _stop_recording(self):
        self.log("stopRecording")
        gen = self._session_id

        if self._recording_started_at:
            elapsed = time.time() - self._recording_started_at
            self._last_duration = max(1, round(elapsed))
        else:
            self._last_duration = None

        self.update_state("transcribing")
        notify("Recording stopped. Transcribing...")

        def on_ffmpeg_exit():
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

        # Transcribe
        self.log("sending file to server...")
        text = transcribe(self.log)

        if gen != self._session_id:
            self.log("transcription callback: stale session")
            return

        if not text:
            self._finish_transcription("No transcription found")
            return

        # Apply replacements
        text = apply_replacements(text)

        # Archive
        self._archive_transcription(text)

        # Clipboard
        if copy_to_clipboard(text):
            self.log("copied to clipboard")
        else:
            self.log("clipboard copy failed")

        # Show result
        preview = text[:60] if len(text) > 60 else text
        self.update_state("complete")
        notify(f"Copied to clipboard\n\n{preview}", title="Transcription complete", timeout_ms=5000)

        self._recording_started_at = None
        self._last_duration = None

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
        if cfg.TEMP_AUDIO_FILE.exists():
            cfg.TEMP_AUDIO_FILE.unlink()
        self.update_state("idle")
        self.server.reset_idle_timer()
        self._transcription_started_for = None
        self._recording_started_at = None
        self._last_duration = None
        if message:
            notify(message)
            self.log(f"finishTranscription: {message}")

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

    def _open_transcript_folder(self):
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

    def _open_dictionary_editor(self):
        """Open the dictionary editor dialog."""
        if self._overlay:
            try:
                self._overlay.show_dictionary_editor()
            except Exception as e:
                self.log(f"dictionary editor error: {e}")
                notify(f"Dictionary editor error: {e}")
        else:
            # Fallback: open the file directly
            try:
                subprocess.Popen(
                    ["xdg-open", str(cfg.DICTIONARY_FILE)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                notify("Could not open dictionary file")

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
        # Stop ffmpeg if still recording
        if self.recorder.is_recording:
            self.log("stopping ffmpeg on shutdown")
            self.recorder.stop()
        self.server.stop()
        self.update_state("idle")
        self.log("daemon stopped")
