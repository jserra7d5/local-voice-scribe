"""Persistent recording history for Linux voice sessions."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import config as cfg

MAX_RECORDINGS = 10


def _metadata_path() -> Path:
    return cfg.RECORDING_HISTORY_DIR / "metadata.json"


def _load_metadata() -> list[dict]:
    path = _metadata_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _save_metadata(items: list[dict]) -> None:
    cfg.RECORDING_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _metadata_path().write_text(json.dumps(items, indent=2, sort_keys=True) + "\n")


def list_recordings() -> list[dict]:
    items = _load_metadata()
    items.sort(key=lambda item: item.get("started_at", 0), reverse=True)
    return items


def get_recording(entry_id: str) -> dict | None:
    for item in _load_metadata():
        if item.get("id") == entry_id:
            return item
    return None


def _trim_to_limit(items: list[dict], limit: int = MAX_RECORDINGS) -> list[dict]:
    items.sort(key=lambda item: item.get("started_at", 0), reverse=True)
    keep = items[:limit]
    remove = items[limit:]
    for item in remove:
        for key in ("audio_path", "transcript_path"):
            raw_path = item.get(key)
            if raw_path:
                path = Path(raw_path)
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
    return keep


def create_recording(audio_source: Path, *, started_at: float, duration_s: int) -> dict | None:
    if not audio_source.exists():
        return None

    cfg.RECORDING_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started_at))
    millis = int((started_at % 1) * 1000)
    entry_id = f"{stamp}_{millis:03d}"
    audio_path = cfg.RECORDING_HISTORY_DIR / f"{entry_id}__dur-{int(duration_s)}s.wav"
    shutil.copy2(audio_source, audio_path)

    entry = {
        "id": entry_id,
        "started_at": started_at,
        "duration_s": int(duration_s),
        "created_at": time.time(),
        "audio_path": str(audio_path),
        "transcript_path": "",
        "transcript_preview": "",
        "status": "archived",
        "error": "",
    }

    items = _load_metadata()
    items = [item for item in items if item.get("id") != entry_id]
    items.append(entry)
    _save_metadata(_trim_to_limit(items))
    return entry


def update_recording(
    entry_id: str,
    *,
    transcript_text: str | None = None,
    status: str | None = None,
    error: str | None = None,
) -> dict | None:
    items = _load_metadata()
    updated = None
    for item in items:
        if item.get("id") != entry_id:
            continue
        if transcript_text is not None:
            transcript_path = cfg.RECORDING_HISTORY_DIR / f"{entry_id}.txt"
            transcript_path.write_text(transcript_text)
            item["transcript_path"] = str(transcript_path)
            preview = transcript_text.replace("\n", " ").strip()
            item["transcript_preview"] = preview[:140]
        if status is not None:
            item["status"] = status
        if error is not None:
            item["error"] = error
        updated = item
        break

    if updated is None:
        return None

    _save_metadata(_trim_to_limit(items))
    return updated
