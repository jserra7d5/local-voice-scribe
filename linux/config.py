"""Configuration loader for local-voice-scribe on Linux."""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".local-voice-scribe"
RUNTIME_FILE = CONFIG_DIR / "runtime.json"
USER_CONFIG_FILE = CONFIG_DIR / "config.json"
DICTIONARY_FILE = CONFIG_DIR / "dictionary.txt"
LOG_FILE = Path("/tmp/whisper_debug.log")
STATE_FILE = Path("/tmp/whisper_state.txt")
TRANSCRIPT_DIR = Path("/tmp/local-voice-scribe-transcripts")
TEMP_AUDIO_FILE = Path("/tmp/whisper_recording.wav")
RECORDING_HISTORY_DIR = CONFIG_DIR / "recordings"
WHISPER_SERVER_PORT = 8178
STATUS_SERVER_PORT = 8989

DEFAULTS = {
    "border_flash_enabled": True,
    "border_color_recording": "#ff2828",
    "border_color_transcribing": "#46aaff",
    "border_color_complete": "#28dc28",
    "duck_enabled": False,
    "duck_level": 10,
    "duck_ramp_down": 0.5,
    "duck_ramp_up": 1.0,
    "duck_rules": [],
    "server_idle_timeout": 300,
    "hotkey_toggle_recording": "super+alt+r",
    "hotkey_open_settings": "super+alt+c",
    "hotkey_open_transcripts": "super+alt+t",
    "audio_device": None,
    "ffmpeg_path": None,
    "whisper_server_path": None,
    "model_path": None,
    "install_token": None,
    "repo_root": None,
    "launcher_path": None,
}

# Keys that are managed by the installer, not the user
RUNTIME_OWNED_KEYS = {
    "ffmpeg_path", "whisper_server_path", "model_path",
    "install_token", "repo_root", "launcher_path",
}


def _coerce_color_hex(value, default: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) == 7 and text.startswith("#") and all(ch in "0123456789abcdef" for ch in text[1:]):
        return text
    return default


def _coerce_duck_rules(value) -> list[dict]:
    """Normalize persisted duck rules to a safe list of dicts."""
    if not isinstance(value, list):
        return []

    rules = []
    for item in value:
        if not isinstance(item, dict):
            continue
        match_binary = str(item.get("match_binary", "")).strip().lower()
        if not match_binary:
            continue

        mode = str(item.get("mode", "custom")).strip().lower()
        if mode not in {"custom", "bypass"}:
            mode = "custom"

        try:
            duck_level = int(item.get("duck_level", DEFAULTS["duck_level"]))
        except (TypeError, ValueError):
            duck_level = DEFAULTS["duck_level"]
        duck_level = max(0, min(100, duck_level))

        rules.append({
            "match_binary": match_binary,
            "display_name": str(item.get("display_name", match_binary)).strip() or match_binary,
            "mode": mode,
            "duck_level": duck_level,
        })
    return rules


def _normalize_config(config: dict) -> dict:
    """Backfill compatibility keys and sanitize persisted values."""
    if not config.get("hotkey_open_settings"):
        legacy = config.get("hotkey_dictionary_editor")
        if legacy:
            config["hotkey_open_settings"] = legacy
    config["border_flash_enabled"] = bool(config.get("border_flash_enabled", DEFAULTS["border_flash_enabled"]))
    config["border_color_recording"] = _coerce_color_hex(
        config.get("border_color_recording"), DEFAULTS["border_color_recording"]
    )
    config["border_color_transcribing"] = _coerce_color_hex(
        config.get("border_color_transcribing"), DEFAULTS["border_color_transcribing"]
    )
    config["border_color_complete"] = _coerce_color_hex(
        config.get("border_color_complete"), DEFAULTS["border_color_complete"]
    )
    config["duck_rules"] = _coerce_duck_rules(config.get("duck_rules"))
    return config


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

    return _normalize_config(config)


def build_user_config_snapshot(config: dict) -> dict:
    """Extract the user-managed config keys for persistence."""
    snapshot = {}
    for key, value in config.items():
        if key in RUNTIME_OWNED_KEYS:
            continue
        if key == "hotkey_dictionary_editor":
            continue
        snapshot[key] = value
    snapshot["duck_rules"] = _coerce_duck_rules(snapshot.get("duck_rules"))
    return _normalize_config(snapshot)


def save_user_config(config: dict):
    """Persist the user-managed config to config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_user_config_snapshot(config)
    USER_CONFIG_FILE.write_text(json.dumps(payload, indent=4, sort_keys=True) + "\n")
