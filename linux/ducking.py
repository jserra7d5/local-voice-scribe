"""Playback stream ducking for Linux via pactl.

Hardened model: ducking only ever *lowers* volume; restoring always returns a
stream to 100% (unity). No "original" volume is ever captured or trusted as the
restore target, so a stream left ducked by a crash, a missed ``end_session``, or
an app restart can never poison a later restore (the bug that left Strawberry /
Spotify permanently quiet).

Crash safety: a marker file (``cfg.DUCK_STATE_FILE``) exists for the duration of
a ducking session. If the daemon dies mid-duck, the next startup sees the marker
and restores every active playback stream to 100%.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

from . import config as cfg
from .notifications import notify

# pactl integer volume for 100% (unity). pactl accepts raw integer values.
FULL_VOLUME = 65536


def _default_runner(args, *, timeout):
    """Default command runner. Tests inject a fake with the same signature."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def _parse_percent(value) -> int:
    if isinstance(value, str) and value.endswith("%"):
        value = value[:-1]
    try:
        return max(0, min(150, round(float(value))))
    except (TypeError, ValueError):
        return 100


def _stream_binary(stream: dict) -> str:
    props = stream.get("properties") or {}
    binary = str(props.get("application.process.binary", "")).strip().lower()
    if binary:
        return binary
    name = str(props.get("application.name", "")).strip().lower()
    return name


def _stream_display_name(stream: dict) -> str:
    props = stream.get("properties") or {}
    return (
        str(props.get("application.name", "")).strip()
        or str(props.get("application.process.binary", "")).strip()
        or f"Stream {stream.get('index', '?')}"
    )


def list_active_streams(runner=None) -> list[dict]:
    """Return active playback streams and metadata needed for ducking/UI.

    ``runner`` is an optional command runner (defaults to a real subprocess);
    tests pass a fake. The return shape is stable — ``settings.py`` consumes it.
    """
    run = runner or _default_runner
    try:
        result = run(
            ["pactl", "--format=json", "list", "sink-inputs"],
            timeout=3,
        )
        raw_streams = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    streams = []
    for item in raw_streams:
        if not isinstance(item, dict):
            continue
        stream_id = item.get("index")
        if stream_id is None:
            continue

        volume = item.get("volume") or {}
        channel_values = []
        channel_percents = []
        for data in volume.values():
            if not isinstance(data, dict):
                continue
            try:
                channel_values.append(int(data.get("value", FULL_VOLUME)))
            except (TypeError, ValueError):
                channel_values.append(FULL_VOLUME)
            channel_percents.append(_parse_percent(data.get("value_percent", "100%")))

        if not channel_values:
            channel_values = [FULL_VOLUME]
        if not channel_percents:
            channel_percents = [100]

        streams.append({
            "id": int(stream_id),
            "binary": _stream_binary(item),
            "display_name": _stream_display_name(item),
            "media_name": str((item.get("properties") or {}).get("media.name", "")).strip(),
            "corked": bool(item.get("corked", False)),
            "channel_values": channel_values,
            "channel_percents": channel_percents,
            "average_percent": round(sum(channel_percents) / len(channel_percents)),
        })
    return streams


class DuckingController:
    """Lower playback volume during recording, always restore to 100%.

    The controller is deliberately stateless about "original" volumes: it never
    stores a per-stream baseline to restore to. Ducking lowers; restoring sets
    everything it would duck back to unity. This makes a permanently-stuck
    ducked stream impossible by construction.
    """

    def __init__(self, app_config: dict, log_fn, runner=None):
        self.config = app_config
        self.log = log_fn
        self._runner = runner or _default_runner
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._session_active = False
        # Bumped on every session boundary; in-flight duck ramps abort when it
        # changes so a late duck step can never fight a restore.
        self._epoch = 0
        self._warned_unavailable = False

    def update_config(self, app_config: dict):
        self.config = app_config

    # ------------------------------------------------------------------ public

    def begin_session(self):
        if not self.config.get("duck_enabled"):
            self.log("ducking disabled")
            return
        if not self._pactl_available():
            self._warn_unavailable_once()
            return

        with self._lock:
            if self._session_active:
                return
            self._session_active = True
            self._epoch += 1
            self._stop_event.clear()

        self._write_marker()
        self.log("ducking session start")
        self._duck_current_streams(ramp_seconds=float(self.config.get("duck_ramp_down", 0.5)))
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def end_session(self):
        with self._lock:
            if not self._session_active:
                return
            self._session_active = False
            self._epoch += 1  # invalidate any in-flight duck ramps
            self._stop_event.set()

        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None

        self.log("ducking session stop; restoring to 100%")
        self._restore_all(ramp_seconds=float(self.config.get("duck_ramp_up", 1.0)))
        self._clear_marker()

    def recover_on_startup(self):
        """Restore streams to 100% if a previous run died mid-duck.

        Runs regardless of ``duck_enabled`` — recovery must work even if the
        user disabled ducking after the crash. No-op when no marker is present,
        so a clean start never overrides deliberately-set volumes.
        """
        if not cfg.DUCK_STATE_FILE.exists():
            return
        self.log("duck marker present at startup; restoring streams to 100%")
        if self._pactl_available():
            self._restore_all(ramp_seconds=0.0)
        self._clear_marker()

    # --------------------------------------------------------------- internals

    def _poll_loop(self):
        # Catch streams that start *during* a recording and duck them too.
        while not self._stop_event.wait(0.25):
            self._duck_current_streams(ramp_seconds=0.0)

    def _duck_current_streams(self, ramp_seconds: float):
        epoch = self._epoch
        for stream in list_active_streams(self._runner):
            target_pct = self._target_percent_for_stream(stream)
            if target_pct >= 100:
                continue  # bypass rule (or nothing to lower)
            target_values = self._scaled_values(stream["channel_values"], target_pct)
            self.log(
                f"duck id={stream['id']} binary={stream['binary']} "
                f"from={stream['channel_values']} to={target_values}"
            )
            self._ramp(
                stream["id"],
                stream["channel_values"],
                target_values,
                ramp_seconds,
                epoch=epoch,
                background=True,
            )

    def _restore_all(self, ramp_seconds: float):
        """Set every stream we would duck back to 100% (re-enumerated live)."""
        for stream in list_active_streams(self._runner):
            if self._target_percent_for_stream(stream) >= 100:
                continue  # bypassed app — we never touched it
            full = [FULL_VOLUME] * len(stream["channel_values"])
            if stream["channel_values"] == full:
                continue  # already at unity
            self.log(
                f"restore id={stream['id']} binary={stream['binary']} "
                f"from={stream['channel_values']} to={full}"
            )
            # Restore is authoritative: epoch=None so it never aborts and always
            # snaps to exactly 100% at the end.
            self._ramp(
                stream["id"],
                stream["channel_values"],
                full,
                ramp_seconds,
                epoch=None,
                background=True,
            )

    def _target_percent_for_stream(self, stream: dict) -> int:
        for rule in self.config.get("duck_rules", []):
            if rule.get("match_binary", "").strip().lower() != stream["binary"]:
                continue
            if rule.get("mode") == "bypass":
                return 100
            return max(0, min(100, int(rule.get("duck_level", self.config.get("duck_level", 10)))))
        return max(0, min(100, int(self.config.get("duck_level", 10))))

    def _scaled_values(self, original_values: list[int], percent: int) -> list[int]:
        return [max(0, round(value * percent / 100.0)) for value in original_values]

    def _ramp(
        self,
        stream_id: int,
        from_values: list[int],
        to_values: list[int],
        ramp_seconds: float,
        *,
        epoch: int | None,
        background: bool,
    ):
        if background:
            threading.Thread(
                target=self._ramp_blocking,
                args=(stream_id, from_values, to_values, ramp_seconds, epoch),
                daemon=True,
            ).start()
            return
        self._ramp_blocking(stream_id, from_values, to_values, ramp_seconds, epoch)

    def _ramp_blocking(
        self,
        stream_id: int,
        from_values: list[int],
        to_values: list[int],
        ramp_seconds: float,
        epoch: int | None,
    ):
        if ramp_seconds <= 0:
            if epoch is not None and self._epoch != epoch:
                return
            self._apply_volume(stream_id, to_values)
            return

        steps = 10
        interval = ramp_seconds / steps
        for step in range(1, steps + 1):
            if epoch is not None and self._epoch != epoch:
                return  # session changed — abandon this duck ramp
            step_values = [
                round(start + (target - start) * (step / steps))
                for start, target in zip(from_values, to_values, strict=False)
            ]
            if len(step_values) < len(to_values):
                step_values.extend(to_values[len(step_values):])
            self._apply_volume(stream_id, step_values)
            if step < steps:
                time.sleep(interval)
        if epoch is not None and self._epoch != epoch:
            return
        self._apply_volume(stream_id, to_values)

    def _apply_volume(self, stream_id: int, channel_values: list[int]):
        volumes = [str(max(0, int(value))) for value in channel_values]
        try:
            self._runner(
                ["pactl", "set-sink-input-volume", str(stream_id), *volumes],
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"set-sink-input-volume failed for #{stream_id}: {exc}")

    def _write_marker(self):
        try:
            cfg.DUCK_STATE_FILE.write_text(json.dumps({"started_at": time.time()}))
        except OSError as exc:
            self.log(f"duck marker write failed: {exc}")

    def _clear_marker(self):
        try:
            cfg.DUCK_STATE_FILE.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"duck marker clear failed: {exc}")

    def _pactl_available(self) -> bool:
        try:
            self._runner(["pactl", "info"], timeout=2)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _warn_unavailable_once(self):
        if self._warned_unavailable:
            return
        self._warned_unavailable = True
        self.log("ducking unavailable: pactl not working")
        notify("Playback ducking is unavailable because pactl could not control audio.", title="Ducking unavailable")
