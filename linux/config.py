"""Configuration loader for local-voice-scribe on Linux."""

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".local-voice-scribe"
RUNTIME_FILE = CONFIG_DIR / "runtime.json"
USER_CONFIG_FILE = CONFIG_DIR / "config.json"
DICTIONARY_FILE = CONFIG_DIR / "dictionary.txt"
LOG_FILE = Path("/tmp/whisper_debug.log")
STATE_FILE = Path("/tmp/whisper_state.txt")
TRANSCRIPT_DIR = Path("/tmp/local-voice-scribe-transcripts")
TEMP_AUDIO_FILE = Path("/tmp/whisper_recording.wav")
WHISPER_SERVER_PORT = 8178
STATUS_SERVER_PORT = 8989

DEFAULTS = {
    "duck_enabled": False,
    "duck_level": 10,
    "duck_ramp_down": 0.5,
    "duck_ramp_up": 1.0,
    "server_idle_timeout": 300,
    "hotkey_toggle_recording": "super+alt+r",
    "hotkey_dictionary_editor": "super+alt+c",
    "hotkey_open_transcripts": "super+alt+t",
    "audio_device": None,  # auto-detect Focusrite Scarlett
    "ffmpeg_path": None,
    "whisper_server_path": None,
    "model_path": None,
    "install_token": None,
    "repo_root": None,
}

# Keys that are managed by the installer, not the user
RUNTIME_OWNED_KEYS = {
    "ffmpeg_path", "whisper_server_path", "model_path",
    "install_token", "repo_root",
}


def load_config() -> dict:
    """Load and merge config from defaults, runtime.json, and config.json."""
    config = dict(DEFAULTS)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Load runtime config (installer-managed)
    if RUNTIME_FILE.exists():
        try:
            runtime = json.loads(RUNTIME_FILE.read_text())
            for k, v in runtime.items():
                config[k] = v
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: runtime config error: {e}")

    # Load user config (user-managed, runtime keys ignored)
    if USER_CONFIG_FILE.exists():
        try:
            user = json.loads(USER_CONFIG_FILE.read_text())
            for k, v in user.items():
                if k in RUNTIME_OWNED_KEYS:
                    print(f"Warning: ignoring installer-owned config key: {k}")
                else:
                    config[k] = v
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: user config error: {e}")

    # Create empty dictionary if missing
    if not DICTIONARY_FILE.exists():
        DICTIONARY_FILE.touch()

    return config
