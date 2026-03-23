# Config System & Transcription Dictionary Editor

**Date:** 2026-03-22
**Status:** Draft

## Overview

Add a user config directory (`~/.local-voice-scribe/`) with a Lua config file and a plain-text transcription dictionary. Add a floating webview editor (Cmd+Alt+C) for managing the dictionary. Feed dictionary words to whisper-server via `initial_prompt` to correct transcription spelling of proper nouns.

Also fix a volume restoration bug where `unduckAudio` doesn't always return to the exact saved volume.

## Config Directory

**Location:** `~/.local-voice-scribe/`

Two files:

### `config.lua`

Returns a Lua table. Missing keys use defaults. Missing file uses all defaults.

```lua
return {
    duck_enabled = true,
    duck_level = 10,
    duck_ramp_down = 0.5,
    duck_ramp_up = 1.0,
    server_idle_timeout = 300,
    hotkey_toggle_recording = { mods = {"cmd", "alt"}, key = "R" },
    hotkey_dictionary_editor = { mods = {"cmd", "alt"}, key = "C" },
}
```

### `dictionary.txt`

One word per line. Blank lines and leading/trailing whitespace are ignored.

```
Quantiiv
ROGER
```

## Dictionary Editor

- **Toggle:** Cmd+Alt+C opens and closes the editor
- **UI:** `hs.webview` floating panel, dark theme, ~400x300px, positioned near top-right of screen
- **Content:** A `<textarea>` pre-populated with current `dictionary.txt` contents
- **Enter key:** Creates newlines (native textarea behavior)
- **Save:** Cmd+S inside the webview or a Save button writes to `dictionary.txt` and closes the panel
- **Dismiss without saving:** Cmd+Alt+C again or Escape closes without saving
- **Feedback:** Brief `hs.alert` on save confirming "Dictionary saved (N words)"

## Whisper Integration

On transcription (in `doTranscription`), read `dictionary.txt` from disk (always fresh, not cached), join all words with ", ", and pass as:

```
-F "initial_prompt=Quantiiv, ROGER, ..."
```

in the curl call to `/inference`. If the dictionary is empty, omit the parameter.

## Config Loading

On Hammerspoon load:

1. Create `~/.local-voice-scribe/` if it doesn't exist
2. If `config.lua` exists, load it with `pcall(dofile, path)`. On error, show `hs.alert` with the error message and fall back to all defaults
3. Merge loaded config with defaults — any missing keys use default values
4. If `dictionary.txt` exists, read it into a word list (cached in memory)
5. Use config values instead of hardcoded constants: `duck_level`, `duck_ramp_down` (in `duckAudio`), `duck_ramp_up` (in `unduckAudio`), `server_idle_timeout`, hotkey bindings
6. If `duck_enabled` is false, `duckAudio()` and `unduckAudio()` are no-ops
7. Bind hotkeys from config

`config.lua` is **not** auto-created — the directory is created but the file is left absent until the user creates it. This avoids confusion about whether defaults live in the file or in code (they live in code). `dictionary.txt` is created empty if missing.

## Volume Restoration Fix

**Bug:** `rampVolume` uses floating-point step math that can land at incorrect values (e.g., 70 instead of 100).

**Fix:** On the final step of `rampVolume`, explicitly call `setAppVolume(appName, toVol)` to snap to the exact target. This fixes drift for both ducking and unducking without hardcoding any value.

## Files Changed

- `init.lua` — config loading, dictionary reading, webview editor, configurable hotkeys, initial_prompt in curl call, rampVolume fix
- `~/.local-voice-scribe/config.lua` — not auto-created; user creates when they want to override defaults
- `~/.local-voice-scribe/dictionary.txt` — created empty (if missing)
- `CLAUDE.md` — updated to document config directory

## Out of Scope

- Post-processing find-and-replace (wrong→right mappings) — future enhancement if `initial_prompt` isn't sufficient
- LLM-based spell correction pass
- Config hot-reloading (requires Hammerspoon reload to pick up config.lua changes; dictionary.txt is read fresh from disk on each transcription)
