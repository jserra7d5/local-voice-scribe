"""Playback stream ducking for Linux via pactl."""

from __future__ import annotations

import json
import subprocess
import threading
import time

from .notifications import notify


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


def list_active_streams() -> list[dict]:
    """Return active playback streams and metadata needed for ducking/UI."""
    try:
        result = subprocess.run(
            ["pactl", "--format=json", "list", "sink-inputs"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
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
                channel_values.append(int(data.get("value", 65536)))
            except (TypeError, ValueError):
                channel_values.append(65536)
            channel_percents.append(_parse_percent(data.get("value_percent", "100%")))

        if not channel_values:
            channel_values = [65536]
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
    """Manage per-stream ducking during active recording sessions."""

    def __init__(self, app_config: dict, log_fn):
        self.config = app_config
        self.log = log_fn
        self._saved_streams: dict[int, dict] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._session_active = False
        self._warned_unavailable = False

    def update_config(self, app_config: dict):
        self.config = app_config

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
            self._saved_streams = {}
            self._stop_event.clear()

        self.log("ducking session start")
        self._apply_current_streams(ramp_seconds=float(self.config.get("duck_ramp_down", 0.5)))
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def end_session(self):
        with self._lock:
            if not self._session_active:
                return
            self._session_active = False
            self._stop_event.set()
            saved = dict(self._saved_streams)
            self._saved_streams = {}

        self.log(f"ducking session stop; restoring {len(saved)} streams")
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None
        self._restore_streams(saved, ramp_seconds=float(self.config.get("duck_ramp_up", 1.0)))

    def _poll_loop(self):
        while not self._stop_event.wait(0.25):
            self._apply_current_streams(ramp_seconds=0.0)

    def _apply_current_streams(self, ramp_seconds: float):
        streams = list_active_streams()
        if not streams:
            return

        for stream in streams:
            with self._lock:
                if stream["id"] in self._saved_streams:
                    continue
                self._saved_streams[stream["id"]] = {
                    "binary": stream["binary"],
                    "display_name": stream["display_name"],
                    "channel_values": list(stream["channel_values"]),
                }

            target_pct = self._target_percent_for_stream(stream)
            target_values = self._scaled_values(stream["channel_values"], target_pct)
            self.log(
                f"duck stream id={stream['id']} binary={stream['binary']} "
                f"from={stream['channel_values']} to={target_values}"
            )
            self._set_stream_volume(
                stream["id"],
                stream["channel_values"],
                target_values,
                ramp_seconds,
                abort_on_stop=True,
                background=True,
            )

    def _restore_streams(self, saved_streams: dict[int, dict], ramp_seconds: float):
        if not saved_streams:
            return

        current_streams = {stream["id"]: stream for stream in list_active_streams()}
        for stream_id, original in saved_streams.items():
            current = current_streams.get(stream_id)
            if not current:
                self.log(f"restore skipped for vanished stream id={stream_id}")
                continue
            from_values = current["channel_values"]
            to_values = original["channel_values"]
            self.log(
                f"restore stream id={stream_id} binary={original['binary']} "
                f"from={from_values} to={to_values}"
            )
            self._set_stream_volume(
                stream_id,
                from_values,
                to_values,
                ramp_seconds,
                abort_on_stop=False,
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

    def _set_stream_volume(
        self,
        stream_id: int,
        from_values: list[int],
        to_values: list[int],
        ramp_seconds: float,
        *,
        abort_on_stop: bool,
        background: bool,
    ):
        if background:
            threading.Thread(
                target=self._ramp_stream_volume,
                args=(stream_id, from_values, to_values, ramp_seconds, abort_on_stop),
                daemon=True,
            ).start()
            return
        self._ramp_stream_volume(stream_id, from_values, to_values, ramp_seconds, abort_on_stop)

    def _ramp_stream_volume(
        self,
        stream_id: int,
        from_values: list[int],
        to_values: list[int],
        ramp_seconds: float,
        abort_on_stop: bool,
    ):
        if ramp_seconds <= 0:
            self._apply_volume(stream_id, to_values)
            return

        steps = 10
        interval = ramp_seconds / steps
        for step in range(1, steps + 1):
            if abort_on_stop and self._stop_event.is_set():
                return
            step_values = [
                round(start + (target - start) * (step / steps))
                for start, target in zip(from_values, to_values, strict=False)
            ]
            if len(step_values) < len(to_values):
                step_values.extend(to_values[len(step_values):])
            self._apply_volume(stream_id, step_values)
            if step < steps:
                time.sleep(interval)
        if abort_on_stop and self._stop_event.is_set():
            return
        self._apply_volume(stream_id, to_values)

    def _apply_volume(self, stream_id: int, channel_values: list[int]):
        volumes = [str(max(0, int(value))) for value in channel_values]
        try:
            subprocess.run(
                ["pactl", "set-sink-input-volume", str(stream_id), *volumes],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"set-sink-input-volume failed for #{stream_id}: {exc}")

    def _pactl_available(self) -> bool:
        try:
            subprocess.run(
                ["pactl", "info"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _warn_unavailable_once(self):
        if self._warned_unavailable:
            return
        self._warned_unavailable = True
        self.log("ducking unavailable: pactl not working")
        notify("Playback ducking is unavailable because pactl could not control audio.", title="Ducking unavailable")
