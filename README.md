# Local Voice Scribe

Hammerspoon-based local voice recording and transcription using `whisper.cpp`.

## One-command setup

Assuming the repo is already cloned:

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

## What gets installed

- Hammerspoon app via Homebrew cask
- `ffmpeg` formula for recording from the default input device
- `cmake` formula for building `whisper.cpp`
- model file at `~/.local-voice-scribe/models/ggml-large-v3-turbo.bin`
- installer-managed runtime config at `~/.local-voice-scribe/runtime.lua`
- source-built `whisper-server` at `~/.local-voice-scribe/whisper/bin/whisper-server`
- copied server public assets at `~/.local-voice-scribe/whisper/public`

## User-managed files

- `~/.local-voice-scribe/config.lua`
  - optional overrides
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

- `Cmd+Alt+R` toggles recording
- `Cmd+Alt+C` opens the floating dictionary editor
- Hammerspoon starts `whisper-server` on load and shuts it down after an idle timeout
- Transcriptions are copied to the clipboard
- Status API runs on `http://127.0.0.1:8989/state`

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
