# Local Voice Scribe

Local voice recording and transcription using `whisper.cpp`. Supports macOS (Hammerspoon) and Linux (Python daemon).

## One-command setup

### macOS

```bash
./scripts/setup.sh --yes
```

The setup script will:

- install Homebrew if needed
- install `ffmpeg`, `cmake`, and `Hammerspoon`
- download and verify the `ggml-large-v3-turbo` model
- download and build a pinned `whisper.cpp` release with `whisper-server` enabled
- write `~/.local-voice-scribe/runtime.lua`
- install or update the managed Local Voice Scribe loader block in `~/.hammerspoon/init.lua`
- restart Hammerspoon and verify the expected install token is live
- verify the status API and `whisper-server`

### Linux

```bash
./scripts/setup-linux.sh --yes
```

The setup script will:

- check for required system packages (`ffmpeg`, `cmake`, `curl`, `xclip`, `nvidia-cuda-toolkit`, etc.)
- download and verify the `ggml-large-v3-turbo` model
- download and build a pinned `whisper.cpp` release with CUDA support
- create a Python virtual environment with `pynput` and `PyQt6`
- detect the Focusrite Scarlett audio device (if connected)
- write `~/.local-voice-scribe/runtime.json`
- create XDG desktop and autostart entries

## What gets installed

### macOS

- Hammerspoon app via Homebrew cask
- `ffmpeg` formula for recording from the default input device
- `cmake` formula for building `whisper.cpp`
- model file at `~/.local-voice-scribe/models/ggml-large-v3-turbo.bin`
- installer-managed runtime config at `~/.local-voice-scribe/runtime.lua`
- source-built `whisper-server` at `~/.local-voice-scribe/whisper/bin/whisper-server`
- copied server public assets at `~/.local-voice-scribe/whisper/public`

### Linux

- `ffmpeg` for recording via PulseAudio/PipeWire
- `cmake` for building `whisper.cpp`
- model file at `~/.local-voice-scribe/models/ggml-large-v3-turbo.bin`
- installer-managed runtime config at `~/.local-voice-scribe/runtime.json`
- source-built `whisper-server` (CUDA) at `~/.local-voice-scribe/whisper/bin/whisper-server`
- Python venv at `~/.local-voice-scribe/venv/`
- launcher script at `local-voice-scribe-linux` (project root)
- XDG desktop entry and autostart entry

## User-managed files

- `~/.local-voice-scribe/config.lua`
  - optional overrides such as `audio_device = "Built-in Microphone"`
  - loaded after the installer-managed runtime config
  - installer-owned path keys are ignored
- `~/.local-voice-scribe/dictionary.txt`
  - one word per line for `initial_prompt`
  - `wrong -> right` replacement rules are also supported

## Hammerspoon policy

- If `~/.hammerspoon/init.lua` is empty or already contains the Local Voice Scribe managed block, setup will write or update it.
- If `~/.hammerspoon/init.lua` already contains unrelated content, setup aborts and prints the exact block to merge manually.
- This is intentional. The installer will not rewrite arbitrary existing Hammerspoon configs.

## Manual approvals

The setup script installs everything it can from the CLI. macOS may still prompt for:

- microphone access on the first recording attempt
- Hammerspoon automation access for Music or Spotify if ducking is used

## Updating or repairing an install

Rerun:

```bash
./scripts/setup.sh --yes
```

This is safe to rerun. It updates the managed Hammerspoon loader block, rewrites `runtime.lua`, re-verifies the model checksum, and rebuilds the pinned `whisper-server` if needed.

For a non-mutating verification pass:

```bash
./scripts/setup.sh --doctor
```

## How it works

### macOS
- `Cmd+Alt+R` toggles recording
- `Cmd+Alt+C` opens the floating dictionary editor and input-device picker
- `Cmd+Alt+T` opens the transcript temp folder in Finder
- Hammerspoon starts `whisper-server` on load and shuts it down after an idle timeout

### Linux
- `Super+Alt+R` toggles recording
- `Super+Alt+C` opens the settings window
- `Super+Alt+T` opens the transcript folder in the file manager
- Daemon starts `whisper-server` on launch and shuts it down after idle timeout
- Floating red dot indicator during recording (PyQt6)
- Settings window exposes ducking, hotkeys, audio input, and dictionary controls

### Both platforms
- Successful transcriptions are copied to the clipboard and archived under `/tmp/local-voice-scribe-transcripts`
- whisper-server keeps the model loaded in GPU memory for sub-second transcription latency
- Dictionary words bias whisper spelling; `wrong -> right` rules post-process the output
- User config: `~/.local-voice-scribe/config.json` (Linux) or `config.lua` (macOS)

## Troubleshooting

- If setup refuses to touch `~/.hammerspoon/init.lua`, merge the printed managed block manually.
- If setup says the status server did not expose the expected install token, Hammerspoon did not load the new config. Check `~/.hammerspoon/init.lua` and `/tmp/whisper_debug.log`.
- If `whisper-server` does not start, check `/tmp/whisper_debug.log`.
- If you move the repo, rerun `./scripts/setup.sh --yes` so the managed loader block points at the new path.

## Docker preflight

The repo includes shell-level installer tests that can run in Docker:

```bash
./scripts/test-installer.sh
```

This validates the installer logic and failure handling. It does not prove the real macOS path, because Docker cannot exercise Hammerspoon, macOS permissions, or `ffmpeg -f avfoundation`.
