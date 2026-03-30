"""Audio recording via ffmpeg with PipeWire/PulseAudio backend."""

import os
import signal
import subprocess
import threading

from . import config as cfg


def detect_focusrite() -> str | None:
    """Detect Focusrite Scarlett source via pactl. Returns source name or None."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=5,
        )
        # Filter for input sources only (alsa_input.*), not output monitors
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                source_name = parts[1]
                if not source_name.startswith("alsa_input."):
                    continue
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
        self._force_killed = False

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

    def start(self, on_crash_callback=None) -> bool:
        """Start recording. Returns True on success.

        If on_crash_callback is provided, it will be called if ffmpeg exits
        unexpectedly (not via stop()).
        """
        ffmpeg = self.config.get("ffmpeg_path") or "ffmpeg"
        if not os.path.exists(ffmpeg) and ffmpeg != "ffmpeg":
            self.log(f"ffmpeg not found: {ffmpeg}")
            return False

        # Remove stale temp file
        if cfg.TEMP_AUDIO_FILE.exists():
            cfg.TEMP_AUDIO_FILE.unlink()

        self._force_killed = False
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

            # Monitor for unexpected crashes
            if on_crash_callback:
                threading.Thread(
                    target=self._crash_monitor,
                    args=(self._process, on_crash_callback),
                    daemon=True,
                ).start()

            return True
        except OSError as e:
            self.log(f"ffmpeg start failed: {e}")
            return False

    def stop(self, on_exit_callback=None):
        """Stop recording with SIGINT (not SIGKILL). Calls callback when ffmpeg exits cleanly."""
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
            was_force_killed = False
            try:
                proc.wait(timeout=2)
                self.log(f"ffmpeg exited cleanly: {proc.returncode}")
            except subprocess.TimeoutExpired:
                self.log("ffmpeg safety timeout — force killing")
                was_force_killed = True
                self._force_killed = True
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass

            self._process = None

            # Only invoke transcription callback if ffmpeg exited via SIGINT (clean WAV)
            # If we had to SIGKILL, the WAV is likely corrupt
            if on_exit_callback and not was_force_killed:
                on_exit_callback()
            elif was_force_killed and on_exit_callback:
                self.log("skipping transcription callback — ffmpeg was force-killed (WAV likely corrupt)")
                # Still call it but the file size check in transcriber will catch bad files

        threading.Thread(target=_wait, daemon=True).start()

    def _crash_monitor(self, proc: subprocess.Popen, callback):
        """Wait for process to exit; if it wasn't stopped intentionally, call crash callback."""
        proc.wait()
        # Only fire crash callback if we still hold this process reference
        # (stop() clears self._process, so if it's still set, this was unexpected)
        if self._process is proc:
            self._process = None
            self.log(f"ffmpeg crashed unexpectedly: exit={proc.returncode}")
            callback()

    @property
    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None
