"""Unit tests for the hardened Linux ducking controller.

Pure stdlib (unittest) — no pytest, no real audio. A fake pactl runner stands
in for subprocess so the tests are deterministic and side-effect free.

Run from the repo root:
    python -m unittest tests.test_ducking
    # or
    python tests/test_ducking.py
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Make the repo root importable regardless of how the test is launched.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linux import config as cfg  # noqa: E402
from linux.ducking import FULL_VOLUME, DuckingController  # noqa: E402


class FakeResult:
    def __init__(self, stdout=""):
        self.stdout = stdout


class FakeRunner:
    """Stand-in for subprocess.run that models pactl's relevant behaviour.

    Maintains in-memory stream state so that ``list sink-inputs`` reflects every
    ``set-sink-input-volume`` already applied — exactly what real restore logic
    relies on when it re-enumerates live streams.
    """

    def __init__(self, streams):
        # streams: list of dicts {id, binary, name, values: [int, ...]}
        self.streams = {s["id"]: dict(s) for s in streams}
        self.set_calls = []  # (stream_id, [values])
        self.pactl_ok = True

    def __call__(self, args, *, timeout):
        if args[:2] == ["pactl", "info"]:
            if not self.pactl_ok:
                raise OSError("pactl unavailable")
            return FakeResult("ok")

        if args[:2] == ["pactl", "--format=json"]:
            return FakeResult(json.dumps(self._streams_json()))

        if args[:2] == ["pactl", "set-sink-input-volume"]:
            stream_id = int(args[2])
            values = [int(v) for v in args[3:]]
            self.set_calls.append((stream_id, values))
            if stream_id in self.streams:
                self.streams[stream_id]["values"] = values
            return FakeResult("")

        raise AssertionError(f"unexpected command: {args}")

    def _streams_json(self):
        out = []
        for s in self.streams.values():
            volume = {}
            for i, v in enumerate(s["values"]):
                volume[f"ch{i}"] = {
                    "value": v,
                    "value_percent": f"{round(v / FULL_VOLUME * 100)}%",
                }
            out.append({
                "index": s["id"],
                "corked": False,
                "volume": volume,
                "properties": {
                    "application.process.binary": s.get("binary", ""),
                    "application.name": s.get("name", s.get("binary", "")),
                    "media.name": s.get("media", ""),
                },
            })
        return out

    def volume_of(self, stream_id):
        return self.streams[stream_id]["values"]


def _wait_until(predicate, timeout=2.0):
    """Poll for a condition because ramps run on daemon threads."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class DuckingTestBase(unittest.TestCase):
    def setUp(self):
        # Redirect the crash-marker file to a throwaway path so we never touch
        # the real /tmp/whisper_duck_state.txt that a live daemon may be using.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_marker = cfg.DUCK_STATE_FILE
        cfg.DUCK_STATE_FILE = Path(self._tmp.name) / "duck_state.txt"

    def tearDown(self):
        cfg.DUCK_STATE_FILE = self._orig_marker
        self._tmp.cleanup()

    def make_controller(self, runner, **overrides):
        config = {
            "duck_enabled": True,
            "duck_level": 25,
            "duck_ramp_down": 0.0,  # instant — deterministic tests
            "duck_ramp_up": 0.0,
            "duck_rules": [],
        }
        config.update(overrides)
        return DuckingController(config, log_fn=lambda *_: None, runner=runner)


class RestoreAlwaysGoesTo100(DuckingTestBase):
    def test_restore_from_already_ducked_stream_reaches_unity(self):
        """The core regression: a stream that *starts* ducked at 23% must end at
        100% after a duck/restore cycle — never back at its (poisoned) start."""
        runner = FakeRunner([
            {"id": 42, "binary": "strawberry", "values": [15155, 15155]},  # 23%
        ])
        ctrl = self.make_controller(runner)

        ctrl.begin_session()
        self.assertTrue(
            _wait_until(lambda: runner.volume_of(42) != [15155, 15155]),
            "stream should have been ducked",
        )
        ctrl.end_session()

        self.assertTrue(
            _wait_until(lambda: runner.volume_of(42) == [FULL_VOLUME, FULL_VOLUME]),
            f"expected restore to 100%, got {runner.volume_of(42)}",
        )

    def test_restore_targets_unity_not_prior_value(self):
        """Even a stream that started at a non-100% user value restores to 100%
        (matches the 'always 100%' decision)."""
        runner = FakeRunner([
            {"id": 7, "binary": "spotify", "values": [40000, 40000]},
        ])
        ctrl = self.make_controller(runner, duck_rules=[
            {"match_binary": "spotify", "mode": "custom", "duck_level": 50},
        ])
        ctrl.begin_session()
        ctrl.end_session()
        self.assertTrue(
            _wait_until(lambda: runner.volume_of(7) == [FULL_VOLUME, FULL_VOLUME]),
            f"expected 100%, got {runner.volume_of(7)}",
        )


class BypassRulesUntouched(DuckingTestBase):
    def test_bypassed_app_is_never_ducked_or_forced(self):
        runner = FakeRunner([
            {"id": 9, "binary": "firefox", "values": [50000, 50000]},
        ])
        ctrl = self.make_controller(runner, duck_rules=[
            {"match_binary": "firefox", "mode": "bypass"},
        ])
        ctrl.begin_session()
        ctrl.end_session()
        time.sleep(0.05)
        self.assertEqual(runner.volume_of(9), [50000, 50000])
        self.assertEqual(runner.set_calls, [], "bypassed app must not be written to")


class CrashRecovery(DuckingTestBase):
    def test_marker_written_during_session_and_cleared_after(self):
        runner = FakeRunner([{"id": 1, "binary": "strawberry", "values": [FULL_VOLUME, FULL_VOLUME]}])
        ctrl = self.make_controller(runner)
        ctrl.begin_session()
        self.assertTrue(cfg.DUCK_STATE_FILE.exists(), "marker should exist mid-session")
        ctrl.end_session()
        self.assertFalse(cfg.DUCK_STATE_FILE.exists(), "marker should be cleared after restore")

    def test_recover_on_startup_restores_when_marker_present(self):
        runner = FakeRunner([{"id": 5, "binary": "strawberry", "values": [3789, 3789]}])  # stuck ducked
        cfg.DUCK_STATE_FILE.write_text(json.dumps({"started_at": 0}))
        ctrl = self.make_controller(runner)
        ctrl.recover_on_startup()
        self.assertEqual(runner.volume_of(5), [FULL_VOLUME, FULL_VOLUME])
        self.assertFalse(cfg.DUCK_STATE_FILE.exists())

    def test_recover_on_startup_noop_without_marker(self):
        runner = FakeRunner([{"id": 5, "binary": "strawberry", "values": [30000, 30000]}])
        # No marker file.
        ctrl = self.make_controller(runner)
        ctrl.recover_on_startup()
        self.assertEqual(runner.volume_of(5), [30000, 30000])
        self.assertEqual(runner.set_calls, [], "clean start must not move volumes")


class MultiChannel(DuckingTestBase):
    def test_surround_stream_restores_all_channels_to_unity(self):
        runner = FakeRunner([
            {"id": 3, "binary": "strawberry", "values": [10000, 10000, 10000, 10000, 10000, 10000]},
        ])
        ctrl = self.make_controller(runner)
        ctrl.begin_session()
        ctrl.end_session()
        self.assertTrue(
            _wait_until(lambda: runner.volume_of(3) == [FULL_VOLUME] * 6),
            f"expected 6×100%, got {runner.volume_of(3)}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
