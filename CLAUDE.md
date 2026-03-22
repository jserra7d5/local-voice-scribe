# Local Voice Scribe

Hammerspoon-based voice recording and transcription tool using whisper.cpp.

## Setup

- `init.lua` is the main (and only) script
- Symlinked to `~/.hammerspoon/init.lua`
- Uses whisper.cpp at `/Users/joeserra/Documents/whisper.cpp/build/bin/whisper-cli`
- Model: `ggml-large-v3-turbo` at `/Users/joeserra/Documents/whisper.cpp/models/`
- Requires ffmpeg (`/opt/homebrew/bin/ffmpeg`) for audio capture

## How it works

- **Cmd+Alt+R** toggles recording on/off
- Records from the system default audio input device via ffmpeg/avfoundation
- Transcribes with whisper.cpp, copies result to clipboard
- Exposes an HTTP API on port 8989 (`/state`, `/toggle`)

## Features

- **Audio ducking**: Lowers Music/Spotify volume to 10% while recording, restores on stop. Duck state is persisted to `/tmp/whisper_duck_state.txt` so volumes are restored even if Hammerspoon crashes/reloads.
- **Border flash indicators**: Red persistent border while recording, yellow flash on stop (transcribing), green flash on transcription complete.
- **Alerts**: Positioned at top of screen via `hs.alert.defaultStyle.atScreenEdge = 1`.

## State files

- `/tmp/whisper_state.txt` — current state (idle/recording/transcribing/complete)
- `/tmp/whisper_duck_state.txt` — saved app volumes during ducking (safety restore)
- `/tmp/whisper_recording.wav` — temporary audio file (deleted after transcription)
