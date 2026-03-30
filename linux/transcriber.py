"""Transcription via whisper-server HTTP API."""

import json
import subprocess

from . import config as cfg
from .dictionary import load_dictionary


def transcribe(log_fn) -> str | None:
    """Send the temp audio file to whisper-server for transcription. Returns text or None."""
    audio_file = cfg.TEMP_AUDIO_FILE
    if not audio_file.exists():
        log_fn("transcribe: audio file missing")
        return None

    size = audio_file.stat().st_size
    log_fn(f"transcribe: file size={size}")
    if size < 1000:
        log_fn("transcribe: recording too short")
        return None

    curl_args = [
        "curl", "-s", "--max-time", "30",
        "-X", "POST",
        "-F", f"file=@{audio_file}",
        "-F", "response_format=json",
    ]

    dict_string = load_dictionary()
    if dict_string:
        curl_args.extend(["-F", f"initial_prompt={dict_string}"])
        log_fn(f"using initial_prompt: {dict_string}")

    curl_args.append(f"http://127.0.0.1:{cfg.WHISPER_SERVER_PORT}/inference")

    log_fn(f"curl args: {curl_args}")

    try:
        result = subprocess.run(
            curl_args,
            capture_output=True, text=True, timeout=35,
        )
    except subprocess.TimeoutExpired:
        log_fn("transcribe: curl timed out")
        return None

    log_fn(f"curl exit={result.returncode}")
    log_fn(f"curl stdout=[{result.stdout}]")

    if result.returncode != 0 or not result.stdout:
        log_fn("transcribe: curl failed")
        return None

    try:
        payload = json.loads(result.stdout)
        text = payload.get("text", "").strip()
        log_fn(f"parsed transcription=[{text}]")
        return text if text else None
    except (json.JSONDecodeError, AttributeError) as e:
        log_fn(f"transcribe: JSON parse error: {e}")
        return None
