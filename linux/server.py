"""Whisper server lifecycle management."""

import os
import signal
import subprocess
import threading
import time
import urllib.request

from . import config as cfg
from .notifications import notify


class WhisperServer:
    """Manages the whisper-server process with idle auto-shutdown."""

    def __init__(self, app_config: dict, log_fn):
        self.config = app_config
        self.log = log_fn
        self._process: subprocess.Popen | None = None
        self._idle_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def is_up(self) -> bool:
        """Check if whisper-server is responding on its port."""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{cfg.WHISPER_SERVER_PORT}/",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=1) as resp:
                up = resp.status == 200
        except Exception:
            up = False
        self.log(f"isServerUp: {up}")
        return up

    def launch_if_needed(self):
        """Start the server if it's not already running."""
        if self.is_up():
            self.log("server already up")
            self.reset_idle_timer()
            return

        server_path = self.config.get("whisper_server_path")
        model_path = self.config.get("model_path")
        if not server_path or not os.path.exists(server_path):
            self.log(f"whisper-server not found: {server_path}")
            notify("whisper-server not found. Run setup-linux.sh.", title="Error")
            return
        if not model_path or not os.path.exists(model_path):
            self.log(f"model not found: {model_path}")
            notify("Whisper model not found. Run setup-linux.sh.", title="Error")
            return

        self.log("launching new server")
        # Kill any stale process on the port
        self._kill_port()

        cmd = [
            server_path,
            "-m", model_path,
            "-l", "en",
            "--port", str(cfg.WHISPER_SERVER_PORT),
            "--host", "127.0.0.1",
        ]
        self.log(f"server cmd: {' '.join(cmd)}")

        with self._lock:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.log(f"server started pid={self._process.pid}")
        self.reset_idle_timer()

    def stop(self):
        """Stop the whisper-server and cancel idle timer."""
        self.log("stopWhisperServer")
        self._cancel_idle_timer()
        with self._lock:
            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=5)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None
        self._kill_port()

    def reset_idle_timer(self):
        """Reset the idle shutdown timer."""
        self._cancel_idle_timer()
        timeout = self.config.get("server_idle_timeout", 300)
        self._idle_timer = threading.Timer(timeout, self._idle_shutdown)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def suspend_idle_timer(self):
        """Suspend the idle timer (during active recording/transcription)."""
        self._cancel_idle_timer()

    def wait_for_ready(self, max_attempts: int = 40, interval: float = 0.5) -> bool:
        """Poll until server is up. Returns True if server came up."""
        for i in range(max_attempts):
            if self.is_up():
                self.log(f"server ready after {i + 1} polls")
                return True
            time.sleep(interval)
        self.log(f"server failed to start after {max_attempts} polls")
        return False

    def _cancel_idle_timer(self):
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _idle_shutdown(self):
        self.log("idle timeout — shutting down server")
        self.stop()

    def _kill_port(self):
        """Kill any process listening on the whisper server port."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{cfg.WHISPER_SERVER_PORT}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                for pid in result.stdout.strip().split("\n"):
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except (ValueError, OSError):
                        pass
        except Exception:
            pass
