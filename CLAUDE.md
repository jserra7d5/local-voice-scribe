# Local Voice Scribe

Local voice recording and transcription tool using whisper.cpp. Dual-platform: macOS (Hammerspoon/Lua) and Linux (Python daemon).

## Setup

### macOS
- Bootstrap with `./scripts/setup.sh --yes`
- Verify with `./scripts/setup.sh --doctor`
- `init.lua` is the macOS entry point (Hammerspoon)
- Hammerspoon loads this repo through a managed block in `~/.hammerspoon/init.lua`
- Installer-managed runtime paths live in `~/.local-voice-scribe/runtime.lua`
- User overrides live in `~/.local-voice-scribe/config.lua`

### Linux
- Bootstrap with `./scripts/setup-linux.sh --yes`
- Verify with `./scripts/setup-linux.sh --doctor`
- `linux/` package is the Linux entry point (Python daemon)
- Run with `./local-voice-scribe-linux` or `python3 -m linux` from repo root
- Installer-managed runtime paths live in `~/.local-voice-scribe/runtime.json`
- User overrides live in `~/.local-voice-scribe/config.json`
- Python venv at `~/.local-voice-scribe/venv/`
- Requires NVIDIA GPU with CUDA for whisper-server acceleration
- Auto-detects Focusrite Scarlett via PipeWire/PulseAudio

### Shared
- Model: `ggml-large-v3-turbo` at `~/.local-voice-scribe/models/ggml-large-v3-turbo.bin`
- Source-built `whisper-server`: `~/.local-voice-scribe/whisper/bin/whisper-server`
- Dictionary: `~/.local-voice-scribe/dictionary.txt`

## How it works

### macOS hotkeys
- **Cmd+Alt+R** toggles recording on/off
- **Cmd+Alt+C** opens dictionary editor
- **Cmd+Alt+T** opens the transcript temp folder

### Linux hotkeys
- **Super+Alt+R** toggles recording on/off
- **Super+Alt+C** opens dictionary editor
- **Super+Alt+T** opens the transcript folder

### Both platforms
- Records from audio input via ffmpeg (avfoundation on macOS, pulse on Linux)
- Transcribes via whisper-server HTTP API on port 8178
- Copies result to clipboard, archives to `/tmp/local-voice-scribe-transcripts/`
- Server auto-starts, shuts down after 5 min idle (configurable)
- macOS exposes a status HTTP API on port 8989 (`/state`, `/toggle`)

## Features

- **Whisper server management**: Model stays loaded in memory between transcriptions for sub-second latency. Auto-starts, auto-shuts down after idle timeout. Cleaned up on Hammerspoon reload/shutdown.
- **Audio ducking**: Lowers Music/Spotify volume to 10% while recording with smooth 0.5s ramp down and 1.0s ramp up. Duck state is persisted to `/tmp/whisper_duck_state.txt` so volumes are restored even if Hammerspoon crashes/reloads.
- **Border flash indicators**: Red persistent border while recording, yellow flash on stop (transcribing), green flash on transcription complete.
- **Alerts**: Positioned at top of screen via `hs.alert.defaultStyle.atScreenEdge = 1`.

## User config: `~/.local-voice-scribe/`

### macOS
- **`config.lua`** (optional) — Lua table overriding behavior settings (`duck_enabled`, `server_idle_timeout`, hotkey bindings, etc.)
- **`runtime.lua`** (installer-managed) — resolved paths for `ffmpeg_path`, `whisper_server_path`, `model_path`, etc.

### Linux
- **`config.json`** (optional) — JSON object overriding behavior settings (`server_idle_timeout`, hotkey bindings, `audio_device`, etc.)
- **`runtime.json`** (installer-managed) — resolved paths for `ffmpeg_path`, `whisper_server_path`, `model_path`, `audio_device`, etc.

### Shared
- **`dictionary.txt`** — one word per line, fed to whisper as `initial_prompt` to bias spelling of proper nouns (e.g., Quantiiv). Supports `wrong -> right` replacement rules. Edited via hotkey editor. Read fresh from disk on each transcription.

## Important: ffmpeg termination

ffmpeg MUST be terminated with SIGINT (not SIGKILL/terminate). SIGKILL produces an empty WAV file with no header, causing whisper to hallucinate "thank you." from silence.
- **macOS**: `hs.task:terminate()` sends SIGKILL — use `kill -INT <pid>` instead.
- **Linux**: Use `subprocess.Popen.send_signal(signal.SIGINT)`, not `.kill()` or `.terminate()`.

## Linux architecture

The Linux daemon (`linux/`) is a Python package mirroring the macOS init.lua feature set:
- `daemon.py` — state machine (idle → recording → transcribing → complete → idle) with session IDs
- `hotkeys.py` — X11 XGrabKey via python-xlib (pynput fallback)
- `recorder.py` — ffmpeg recording via PulseAudio backend with Focusrite auto-detection
- `server.py` — whisper-server lifecycle (launch, health check, idle shutdown)
- `transcriber.py` — HTTP POST to whisper-server `/inference` endpoint
- `overlay.py` — PyQt6 floating recording indicator (red dot) + dictionary editor dialog
- `config.py`, `clipboard.py`, `notifications.py` — system integration

## State files

- `/tmp/whisper_state.txt` — current state (idle/recording/transcribing/complete)
- `/tmp/whisper_duck_state.txt` — saved app volumes during ducking (safety restore)
- `/tmp/whisper_recording.wav` — temporary audio file (deleted after transcription)
- `/tmp/local-voice-scribe-transcripts/` — archived successful transcripts as `transcript_YYYY-MM-DD_HH-MM-SS__dur-NNs.txt`
- `/tmp/whisper_debug.log` — debug log (cleared on each Hammerspoon reload)
