"""Audio recording via ffmpeg with PipeWire/PulseAudio backend."""

import os
import signal
import subprocess
import threading
import time

from . import config as cfg


def detect_focusrite() -> str | None:
    """Detect Focusrite Scarlett source via pactl. Returns source name or None."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                source_name = parts[1]
                if "scarlett" in source_name.lower() or "focusrite" in source_name.lower():
                    # Prefer "analog-stereo" variant
                    if "analog-stereo" in source_name.lower():
                        return source_name
        # Second pass: any Focusrite/Scarlett source
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                source_name = parts[1]
                if "scarlett" in source_name.lower() or "focusrite" in source_name.lower():
                    return source_name
    except Exception:
        pass
    return None


class Recorder:
    """Records audio from a PulseAudio/PipeWire source via ffmpeg."""

    def __init__(self, app_config: dict, log_fn):
        self.config = app_config
        self.log = log_fn
        self._process: subprocess.Popen | None = None
        self._safety_timer: threading.Timer | None = None

    @property
    def audio_device(self) -> str:
        """Get the configured audio device, falling back to auto-detect or default."""
        device = self.config.get("audio_device")
        if device:
            return device
        detected = detect_focusrite()
        if detected:
            self.log(f"auto-detected Focusrite: {detected}")
            return detected
        self.log("no Focusrite found, using default")
        return "default"

    def start(self) -> bool:
        """Start recording. Returns True on success."""
        ffmpeg = self.config.get("ffmpeg_path") or "ffmpeg"
        if not os.path.exists(ffmpeg) and ffmpeg != "ffmpeg":
            self.log(f"ffmpeg not found: {ffmpeg}")
            return False

        # Remove stale temp file
        if cfg.TEMP_AUDIO_FILE.exists():
            cfg.TEMP_AUDIO_FILE.unlink()

        device = self.audio_device
        cmd = [
            ffmpeg, "-y",
            "-f", "pulse",
            "-i", device,
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            str(cfg.TEMP_AUDIO_FILE),
        ]
        self.log(f"ffmpeg cmd: {' '.join(cmd)}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.log(f"ffmpeg started pid={self._process.pid}")
            return True
        except OSError as e:
            self.log(f"ffmpeg start failed: {e}")
            return False

    def stop(self, on_exit_callback=None):
        """Stop recording with SIGINT (not SIGKILL). Calls callback when ffmpeg exits."""
        if not self._process:
            return

        pid = self._process.pid
        self.log(f"sending SIGINT to ffmpeg pid={pid}")

        try:
            self._process.send_signal(signal.SIGINT)
        except OSError:
            pass

        # Wait for exit in a thread so we don't block
        def _wait():
            proc = self._process
            try:
                proc.wait(timeout=2)
                self.log(f"ffmpeg exited cleanly: {proc.returncode}")
            except subprocess.TimeoutExpired:
                self.log("ffmpeg safety timeout — force killing")
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
            finally:
                self._process = None
                if self._safety_timer:
                    self._safety_timer.cancel()
                    self._safety_timer = None
                if on_exit_callback:
                    on_exit_callback()

        threading.Thread(target=_wait, daemon=True).start()

    @property
    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None
