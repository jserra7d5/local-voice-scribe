# Local Voice Scribe

Hammerspoon-based voice recording and transcription tool using whisper.cpp.

## Setup

- Bootstrap with `./scripts/setup.sh --yes`
- Verify with `./scripts/setup.sh --doctor`
- `init.lua` is the main (and only) script
- Hammerspoon loads this repo through a managed block in `~/.hammerspoon/init.lua`
- Setup refuses to rewrite unmanaged `~/.hammerspoon/init.lua` files
- Installer-managed runtime paths live in `~/.local-voice-scribe/runtime.lua`
- User overrides live in `~/.local-voice-scribe/config.lua`
- Model: `ggml-large-v3-turbo` at `~/.local-voice-scribe/models/ggml-large-v3-turbo.bin`
- Source-built `whisper-server`: `~/.local-voice-scribe/whisper/bin/whisper-server`
- Whisper public assets: `~/.local-voice-scribe/whisper/public`
- Requires `ffmpeg`, `cmake`, and `Hammerspoon`, installed by `scripts/setup.sh`

## How it works

- **Cmd+Alt+R** toggles recording on/off
- Records from the system default audio input device via ffmpeg/avfoundation
- Transcribes via whisper-server HTTP API, copies result to clipboard
- Server auto-starts on Hammerspoon load, shuts down after 5 min idle (configurable)
- Exposes a status HTTP API on port 8989 (`/state`, `/toggle`)

## Features

- **Whisper server management**: Model stays loaded in memory between transcriptions for sub-second latency. Auto-starts, auto-shuts down after idle timeout. Cleaned up on Hammerspoon reload/shutdown.
- **Audio ducking**: Lowers Music/Spotify volume to 10% while recording with smooth 0.5s ramp down and 1.0s ramp up. Duck state is persisted to `/tmp/whisper_duck_state.txt` so volumes are restored even if Hammerspoon crashes/reloads.
- **Border flash indicators**: Red persistent border while recording, yellow flash on stop (transcribing), green flash on transcription complete.
- **Alerts**: Positioned at top of screen via `hs.alert.defaultStyle.atScreenEdge = 1`.

## User config: `~/.local-voice-scribe/`

- **`config.lua`** (optional) — returns a Lua table to override behavior settings such as `duck_enabled`, `duck_level`, `duck_ramp_down`, `duck_ramp_up`, `server_idle_timeout`, `hotkey_toggle_recording`, and `hotkey_dictionary_editor`. Installer-owned path keys in this file are ignored.
- **`runtime.lua`** (installer-managed) — returns a Lua table with resolved paths for `ffmpeg_path`, `whisper_server_path`, `whisper_public_path`, `model_path`, `install_token`, `repo_root`, and `ggml_metal_path_resources`.
- **`dictionary.txt`** — one word per line, fed to whisper as `initial_prompt` to bias spelling of proper nouns (e.g., Quantiiv). Edited via **Cmd+Alt+C** floating editor. Read fresh from disk on each transcription.

## Important: ffmpeg termination

ffmpeg MUST be terminated with SIGINT (not SIGKILL/terminate). SIGKILL produces an empty WAV file with no header, causing whisper to hallucinate "thank you." from silence. `hs.task:terminate()` sends SIGKILL — use `kill -INT <pid>` instead.

## State files

- `/tmp/whisper_state.txt` — current state (idle/recording/transcribing/complete)
- `/tmp/whisper_duck_state.txt` — saved app volumes during ducking (safety restore)
- `/tmp/whisper_recording.wav` — temporary audio file (deleted after transcription)
- `/tmp/whisper_debug.log` — debug log (cleared on each Hammerspoon reload)
