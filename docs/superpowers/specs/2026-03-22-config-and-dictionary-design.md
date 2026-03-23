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

On transcription (in `doTranscription`), read `dictionary.txt`, join all words with ", ", and pass as:

```
-F "initial_prompt=Quantiiv, ROGER, ..."
```

in the curl call to `/inference`. If the dictionary is empty, omit the parameter.

## Config Loading

On Hammerspoon load:

1. Create `~/.local-voice-scribe/` if it doesn't exist
2. If `config.lua` exists, `dofile()` it and merge with defaults
3. If `dictionary.txt` exists, read it into a word list
4. Use config values instead of hardcoded constants throughout `init.lua`
5. Bind hotkeys from config

## Volume Restoration Fix

**Bug:** `unduckAudio` ramps back to the saved volume using floating-point step math, which can land at values like 70 instead of 100.

**Fix:** User always runs audio at max volume. Instead of ramping back to the saved value, `unduckAudio` should ramp back to 100 (max). Additionally, after the ramp completes (final step), explicitly set the volume to the exact target value to guarantee it lands correctly.

## Files Changed

- `init.lua` — config loading, dictionary reading, webview editor, configurable hotkeys, initial_prompt in curl call, rampVolume fix
- `~/.local-voice-scribe/config.lua` — created with defaults (if missing)
- `~/.local-voice-scribe/dictionary.txt` — created empty (if missing)
- `CLAUDE.md` — updated to document config directory

## Out of Scope

- Post-processing find-and-replace (wrong→right mappings) — future enhancement if `initial_prompt` isn't sufficient
- LLM-based spell correction pass
- Config hot-reloading (requires Hammerspoon reload to pick up config changes; dictionary is reloaded on each save from editor)
