-- Voice Recording & Whisper Transcription Script
-- Hotkey: Cmd+Alt+R (toggle recording on/off)

-- Config directory
local configDir = os.getenv("HOME") .. "/.local-voice-scribe"
local configFile = configDir .. "/config.lua"
local dictionaryFile = configDir .. "/dictionary.txt"

local defaults = {
    duck_enabled = true,
    duck_level = 10,
    duck_ramp_down = 0.5,
    duck_ramp_up = 1.0,
    server_idle_timeout = 300,
    hotkey_toggle_recording = { mods = {"cmd", "alt"}, key = "R" },
    hotkey_dictionary_editor = { mods = {"cmd", "alt"}, key = "C" },
}

-- Load user config
hs.fs.mkdir(configDir)
local config = {}
for k, v in pairs(defaults) do config[k] = v end

if hs.fs.attributes(configFile) then
    local ok, userConfig = pcall(dofile, configFile)
    if ok and type(userConfig) == "table" then
        for k, v in pairs(userConfig) do config[k] = v end
    elseif not ok then
        hs.alert.show("Config error: " .. tostring(userConfig), 10)
    end
end

-- Create empty dictionary if missing
if not hs.fs.attributes(dictionaryFile) then
    local f = io.open(dictionaryFile, "w")
    if f then f:close() end
end

local recordingTask = nil
local tempAudioFile = "/tmp/whisper_recording.wav"
local whisperServerPath = "/Users/joeserra/Documents/whisper.cpp/build/bin/whisper-server"
local modelPath = "/Users/joeserra/Documents/whisper.cpp/models/ggml-large-v3-turbo.bin"
local whisperServerPort = 8178
local stateFile = "/tmp/whisper_state.txt"
local duckStateFile = "/tmp/whisper_duck_state.txt"
local logFile = "/tmp/whisper_debug.log"
local currentState = "idle"
local sessionId = 0
local idleResetTimer = nil
local ffmpegSafetyTimer = nil
local serverPollTimer = nil

local function log(msg)
    local file = io.open(logFile, "a")
    if file then
        file:write(os.date("%H:%M:%S") .. " " .. msg .. "\n")
        file:close()
    end
end

do
    local file = io.open(logFile, "w")
    if file then file:write("=== Hammerspoon reload " .. os.date() .. " ===\n"); file:close() end
end

hs.alert.defaultStyle.atScreenEdge = 1

-- Border visual effects
local borderCanvas = nil
local borderFadeTimer = nil
local borderGeneration = 0

local borderColors = {
    recording = { red = 1, green = 0.15, blue = 0.15 },
    transcribing = { red = 1, green = 0.8, blue = 0 },
    complete = { red = 0.15, green = 0.85, blue = 0.15 },
}

local function clearBorder()
    if borderFadeTimer then borderFadeTimer:stop(); borderFadeTimer = nil end
    if borderCanvas then borderCanvas:delete(); borderCanvas = nil end
end

local function createBorder(color, alpha)
    clearBorder()
    borderGeneration = borderGeneration + 1
    local screen = hs.screen.mainScreen():fullFrame()
    local thickness = 6
    borderCanvas = hs.canvas.new(screen)
    borderCanvas:level(hs.canvas.windowLevels.overlay)
    borderCanvas:behavior(hs.canvas.windowBehaviors.canJoinAllSpaces)
    local fillColor = { red = color.red, green = color.green, blue = color.blue, alpha = alpha }
    local noStroke = { red = 0, green = 0, blue = 0, alpha = 0 }
    borderCanvas:appendElements(
        { type = "rectangle", frame = { x = 0, y = 0, w = screen.w, h = thickness }, fillColor = fillColor, strokeColor = noStroke },
        { type = "rectangle", frame = { x = 0, y = screen.h - thickness, w = screen.w, h = thickness }, fillColor = fillColor, strokeColor = noStroke },
        { type = "rectangle", frame = { x = 0, y = 0, w = thickness, h = screen.h }, fillColor = fillColor, strokeColor = noStroke },
        { type = "rectangle", frame = { x = screen.w - thickness, y = 0, w = thickness, h = screen.h }, fillColor = fillColor, strokeColor = noStroke }
    )
    borderCanvas:show()
    return borderGeneration
end

local function showBorder(colorName) createBorder(borderColors[colorName], 0.9) end

local function flashBorder(colorName)
    local color = borderColors[colorName]
    local gen = createBorder(color, 0.9)
    local steps = 11
    local step = 0
    borderFadeTimer = hs.timer.doEvery(0.05, function(timer)
        step = step + 1
        if step >= steps or gen ~= borderGeneration or not borderCanvas then
            timer:stop()
            if gen == borderGeneration then clearBorder() end
            return
        end
        local alpha = 0.9 * (1 - step / steps)
        for i = 1, borderCanvas:elementCount() do
            borderCanvas:elementAttribute(i, "fillColor", { red = color.red, green = color.green, blue = color.blue, alpha = alpha })
        end
    end)
end

-- Audio ducking
local savedVolumes = {}

local function getAppVolume(appName)
    local ok, vol = hs.osascript.applescript('tell application "' .. appName .. '" to get sound volume')
    if ok then return vol end
    return nil
end

local function setAppVolume(appName, vol)
    hs.osascript.applescript('tell application "' .. appName .. '" to set sound volume to ' .. math.floor(vol))
end

local function isAppRunning(appName) return hs.application.get(appName) ~= nil end

local function saveDuckState()
    local file = io.open(duckStateFile, "w")
    if file then
        for app, vol in pairs(savedVolumes) do file:write(app .. "=" .. vol .. "\n") end
        file:close()
    end
end

local function clearDuckState() os.remove(duckStateFile); savedVolumes = {} end

local function restoreDuckState()
    local file = io.open(duckStateFile, "r")
    if not file then return end
    for line in file:lines() do
        local app, vol = line:match("(.+)=(%d+)")
        if app and vol and isAppRunning(app) then setAppVolume(app, tonumber(vol)) end
    end
    file:close()
    clearDuckState()
end

local duckRampTimers = {}

local function rampVolume(appName, fromVol, toVol, duration, onComplete)
    if duckRampTimers[appName] then duckRampTimers[appName]:stop(); duckRampTimers[appName] = nil end
    local steps = 10
    local interval = duration / steps
    local step = 0
    duckRampTimers[appName] = hs.timer.doEvery(interval, function(timer)
        step = step + 1
        setAppVolume(appName, fromVol + (toVol - fromVol) * (step / steps))
        if step >= steps then
            setAppVolume(appName, toVol)
            timer:stop(); duckRampTimers[appName] = nil
            if onComplete then onComplete() end
        end
    end)
end

local function duckAudio()
    if not config.duck_enabled then return end
    savedVolumes = {}
    for _, appName in ipairs({"Music", "Spotify"}) do
        if isAppRunning(appName) then
            local vol = getAppVolume(appName)
            if vol and vol > 0 then
                savedVolumes[appName] = vol
                rampVolume(appName, vol, vol * (config.duck_level / 100), config.duck_ramp_down)
            end
        end
    end
    if next(savedVolumes) then saveDuckState() end
end

local function unduckAudio()
    if not config.duck_enabled then return end
    local appsToRamp = 0
    local appsRamped = 0
    for appName, vol in pairs(savedVolumes) do
        if isAppRunning(appName) then
            appsToRamp = appsToRamp + 1
            rampVolume(appName, getAppVolume(appName) or (vol * (config.duck_level / 100)), vol, config.duck_ramp_up, function()
                appsRamped = appsRamped + 1
                if appsRamped >= appsToRamp then
                    clearDuckState()
                end
            end)
        end
    end
    if appsToRamp == 0 then clearDuckState() end
end

restoreDuckState()

-- Whisper server
local whisperServerTask = nil
local whisperIdleTimer = nil

local function isServerUp()
    local output = hs.execute("curl -s -o /dev/null -w '%{http_code}' --max-time 1 http://127.0.0.1:" .. whisperServerPort .. "/ 2>/dev/null")
    local up = output and output:match("200") ~= nil
    log("isServerUp: output=[" .. tostring(output) .. "] up=" .. tostring(up))
    return up
end

local function stopWhisperServer()
    log("stopWhisperServer")
    if whisperIdleTimer then whisperIdleTimer:stop(); whisperIdleTimer = nil end
    if whisperServerTask then whisperServerTask:terminate(); whisperServerTask = nil end
    hs.execute("lsof -ti:" .. whisperServerPort .. " | xargs kill 2>/dev/null", true)
end

local function resetServerIdleTimer()
    if whisperIdleTimer then whisperIdleTimer:stop() end
    whisperIdleTimer = hs.timer.doAfter(config.server_idle_timeout, stopWhisperServer)
end

local function suspendServerIdleTimer()
    if whisperIdleTimer then whisperIdleTimer:stop(); whisperIdleTimer = nil end
end

local function launchServerIfNeeded()
    if isServerUp() then
        log("server already up")
        resetServerIdleTimer()
        return
    end

    log("launching new server")
    hs.execute("lsof -ti:" .. whisperServerPort .. " | xargs kill -9 2>/dev/null", true)

    whisperServerTask = hs.task.new("/bin/sh", function(exitCode)
        log("whisper-server exited: " .. tostring(exitCode))
        whisperServerTask = nil
    end, {
        "-c", whisperServerPath .. " -m " .. modelPath .. " -l en --port " .. tostring(whisperServerPort) .. " --host 127.0.0.1 >/dev/null 2>&1"
    })
    whisperServerTask:start()
    log("server task started")
    resetServerIdleTimer()
end

-- Pre-warm on load
launchServerIfNeeded()

local function updateState(state)
    currentState = state
    local file = io.open(stateFile, "w")
    if file then file:write(state); file:close() end
end

local httpServer = hs.httpserver.new(false, false)
httpServer:setPort(8989)
httpServer:setCallback(function(method, path, headers, body)
    if path == "/state" then
        return '{"state":"' .. currentState .. '"}', 200, {["Content-Type"] = "application/json"}
    elseif path == "/toggle" then
        toggleRecording()
        return '{"status":"ok"}', 200, {["Content-Type"] = "application/json"}
    else
        return "Not found", 404, {}
    end
end)
httpServer:start()

local function loadDictionary()
    local file = io.open(dictionaryFile, "r")
    if not file then return nil end
    local words = {}
    for line in file:lines() do
        local word = line:match("^%s*(.-)%s*$")
        if word and #word > 0 and not word:match("->") then
            table.insert(words, word)
        end
    end
    file:close()
    if #words == 0 then return nil end
    return table.concat(words, ", ")
end

local function loadReplacements()
    local file = io.open(dictionaryFile, "r")
    if not file then return {} end
    local replacements = {}
    for line in file:lines() do
        local wrong, right = line:match("^%s*(.-)%s*->%s*(.-)%s*$")
        if wrong and right and #wrong > 0 and #right > 0 then
            table.insert(replacements, { wrong = wrong, right = right })
        end
    end
    file:close()
    return replacements
end

local function escapeLuaPattern(s)
    return s:gsub("[%(%)%.%%%+%-%*%?%[%]%^%$]", "%%%1")
end

local function caseInsensitivePattern(pattern)
    local result = pattern:gsub("%a", function(c)
        return "[" .. c:upper() .. c:lower() .. "]"
    end)
    return result
end

local function applyReplacements(text)
    local replacements = loadReplacements()
    for _, r in ipairs(replacements) do
        local ok, result = pcall(function()
            local pat = caseInsensitivePattern(escapeLuaPattern(r.wrong))
            local rep = r.right:gsub("%%", "%%%%")
            return text:gsub(pat, rep)
        end)
        if ok then
            text = result
        else
            log("replacement error for '" .. r.wrong .. "': " .. tostring(result))
        end
    end
    return text
end

-- Centralized transcription cleanup
local function finishTranscription(message)
    if hs.fs.attributes(tempAudioFile) then os.remove(tempAudioFile) end
    clearBorder()
    updateState("idle")
    resetServerIdleTimer()
    if message then hs.alert.show(message, 3) end
end

local function startRecording()
    sessionId = sessionId + 1
    local gen = sessionId
    updateState("recording")
    log("startRecording session=" .. gen)

    -- Cancel any pending idle reset from previous complete state
    if idleResetTimer then idleResetTimer:stop(); idleResetTimer = nil end

    -- Keep server alive during active work
    suspendServerIdleTimer()

    -- Pre-warm server so it's ready when recording stops
    launchServerIfNeeded()
    suspendServerIdleTimer() -- launchServerIfNeeded arms it, suspend again

    if hs.fs.attributes(tempAudioFile) then os.remove(tempAudioFile) end

    duckAudio()
    showBorder("recording")
    hs.alert.show("Recording started")

    recordingTask = hs.task.new("/opt/homebrew/bin/ffmpeg", function(exitCode)
        log("ffmpeg exited: " .. tostring(exitCode) .. " session=" .. gen)
        if gen ~= sessionId then log("ffmpeg callback: stale session, ignoring"); return end

        -- If we're still in recording state, ffmpeg crashed unexpectedly
        if currentState == "recording" then
            log("ffmpeg crashed unexpectedly during recording")
            unduckAudio()
            finishTranscription("Recording failed (ffmpeg crashed)")
            return
        end

        -- Normal exit after SIGINT — trigger transcription
        if currentState == "transcribing" then
            if ffmpegSafetyTimer then ffmpegSafetyTimer:stop(); ffmpegSafetyTimer = nil end
            local tok, terr = pcall(doTranscription, gen)
            if not tok then
                log("doTranscription ERROR: " .. tostring(terr))
                finishTranscription("Transcription error: " .. tostring(terr))
            end
        end
    end, {
        "-y", "-f", "avfoundation", "-i", ":default",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        tempAudioFile
    })

    if not recordingTask then
        log("ERROR: failed to create ffmpeg task")
        unduckAudio()
        finishTranscription("Recording failed (could not start ffmpeg)")
        return
    end

    recordingTask:start()
    log("ffmpeg started pid=" .. tostring(recordingTask:pid()))
end

function doTranscription(gen)
    if gen ~= sessionId then log("doTranscription: stale session, ignoring"); return end

    local attrs = hs.fs.attributes(tempAudioFile)
    log("doTranscription: file size=" .. tostring(attrs and attrs.size))

    if not attrs or attrs.size < 1000 then
        finishTranscription("Recording too short")
        return
    end

    -- Wait for server, launching if needed
    if not isServerUp() then
        log("server not up, launching and waiting...")
        launchServerIfNeeded()
        suspendServerIdleTimer()
        local attempts = 0
        if serverPollTimer then serverPollTimer:stop() end
        serverPollTimer = hs.timer.doEvery(0.5, function(timer)
            if gen ~= sessionId then timer:stop(); serverPollTimer = nil; return end
            attempts = attempts + 1
            if isServerUp() then
                timer:stop(); serverPollTimer = nil
                log("server came up after " .. attempts .. " polls")
                doTranscription(gen)
            elseif attempts > 40 then
                timer:stop(); serverPollTimer = nil
                log("server failed to start after 40 polls")
                finishTranscription("Whisper server failed to start")
            end
        end)
        return
    end

    log("sending file to server...")
    local dictString = loadDictionary()
    local curlArgs = {
        "-s", "--max-time", "30",
        "-X", "POST",
        "-F", "file=@" .. tempAudioFile,
        "-F", "response_format=json",
    }
    if dictString then
        table.insert(curlArgs, "-F")
        table.insert(curlArgs, "initial_prompt=" .. dictString)
        log("using initial_prompt: " .. dictString)
    end
    table.insert(curlArgs, "http://127.0.0.1:" .. whisperServerPort .. "/inference")

    local curlTask = hs.task.new("/usr/bin/curl", function(exitCode, stdOut, stdErr)
        if gen ~= sessionId then log("curl callback: stale session, ignoring"); return end

        log("curl exit=" .. tostring(exitCode))
        log("curl stdout=[" .. tostring(stdOut) .. "]")
        log("curl stderr=[" .. tostring(stdErr) .. "]")

        if exitCode ~= 0 or not stdOut or #stdOut == 0 then
            finishTranscription("Transcription failed")
            return
        end

        local transcription = stdOut:match('"text"%s*:%s*"(.-)"')
        if transcription then
            transcription = transcription:gsub("\\n", " "):gsub("\\(.)", "%1")
            transcription = transcription:match("^%s*(.-)%s*$")
        end
        log("parsed transcription=[" .. tostring(transcription) .. "]")

        if transcription and #transcription > 0 then
            transcription = applyReplacements(transcription)
            hs.pasteboard.setContents(transcription)
            local preview = transcription
            if #preview > 60 then preview = preview:sub(1, 60) end
            updateState("complete")
            flashBorder("complete")
            hs.alert.show("Copied to clipboard\n\n" .. preview, 5)
            idleResetTimer = hs.timer.doAfter(3, function()
                if gen ~= sessionId then return end
                updateState("idle")
            end)
        else
            finishTranscription("No transcription found")
            return
        end

        if hs.fs.attributes(tempAudioFile) then os.remove(tempAudioFile) end
        resetServerIdleTimer()
    end, curlArgs)
    curlTask:start()
    log("curl started")
end

local function stopRecording()
    log("stopRecording")
    local gen = sessionId

    -- Update state BEFORE sending SIGINT so ffmpeg callback knows this is intentional
    unduckAudio()
    flashBorder("transcribing")
    updateState("transcribing")
    hs.alert.show("Recording stopped. Transcribing")

    if recordingTask then
        local pid = recordingTask:pid()
        log("sending SIGINT to ffmpeg pid=" .. tostring(pid))
        if pid then
            hs.execute("kill -INT " .. pid, true)
        end
    end

    -- Safety timeout: force-kill ffmpeg if it hasn't exited after 2s
    ffmpegSafetyTimer = hs.timer.doAfter(2, function()
        if gen ~= sessionId then return end
        log("ffmpeg safety timeout — force killing")
        if recordingTask then
            local pid = recordingTask:pid()
            if pid then hs.execute("kill -9 " .. pid, true) end
            recordingTask = nil
        end
        -- Trigger transcription directly since callback may not fire
        local ok, err = pcall(doTranscription, gen)
        if not ok then
            log("doTranscription ERROR after safety timeout: " .. tostring(err))
            finishTranscription("Transcription error: " .. tostring(err))
        end
    end)
end

function toggleRecording()
    if currentState == "idle" or currentState == "complete" then
        -- Cancel pending idle reset if transitioning from complete
        if idleResetTimer then idleResetTimer:stop(); idleResetTimer = nil end
        startRecording()
    elseif currentState == "recording" then
        stopRecording()
    else
        log("ignoring toggle, state=" .. currentState)
    end
end

-- Dictionary editor webview
local dictWebview = nil

local function closeDictEditor()
    if dictWebview then dictWebview:delete(); dictWebview = nil end
end

hs.urlevent.bind("dict-save", function(eventName, params)
    log("urlevent dict-save received")
    local data = params.data or ""
    local file = io.open(dictionaryFile, "w")
    if file then
        file:write(data)
        file:close()
        local count = 0
        for line in data:gmatch("[^\n]+") do
            if line:match("%S") then count = count + 1 end
        end
        hs.alert.show("Dictionary saved (" .. count .. " words)")
    end
    closeDictEditor()
end)

hs.urlevent.bind("dict-cancel", function()
    log("urlevent dict-cancel received")
    closeDictEditor()
end)

local function readDictionaryRaw()
    local file = io.open(dictionaryFile, "r")
    if not file then return "" end
    local content = file:read("*a")
    file:close()
    return content
end

local function toggleDictionaryEditor()
    log("toggleDictionaryEditor called")
    local ok, err = pcall(function()
    if dictWebview then
        closeDictEditor()
        return
    end

    local screen = hs.screen.mainScreen():frame()
    local w, h = 400, 300
    local rect = hs.geometry.rect(screen.x + (screen.w - w) / 2, screen.y + (screen.h - h) / 2, w, h)

    local content = readDictionaryRaw():gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"):gsub('"', "&quot;"):gsub("'", "&#39;")

    local html = [[
<!DOCTYPE html>
<html>
<head>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        background: #1e1e1e; color: #d4d4d4; font-family: -apple-system, sans-serif;
        padding: 12px; display: flex; flex-direction: column; height: 100vh;
    }
    h3 { font-size: 13px; margin-bottom: 8px; color: #888; font-weight: 500; }
    textarea {
        flex: 1; background: #2d2d2d; color: #d4d4d4; border: 1px solid #444;
        border-radius: 4px; padding: 8px; font-size: 14px; font-family: 'SF Mono', Menlo, monospace;
        resize: none; outline: none;
    }
    textarea:focus { border-color: #666; }
    .buttons { margin-top: 8px; display: flex; gap: 8px; justify-content: flex-end; }
    button {
        padding: 6px 16px; border: none; border-radius: 4px; font-size: 13px; cursor: pointer;
    }
    .save { background: #2ea043; color: white; }
    .save:hover { background: #3fb950; }
    .cancel { background: #444; color: #ccc; }
    .cancel:hover { background: #555; }
</style>
</head>
<body>
<h3>Whisper Dictionary (one word per line, or: wrong -> right)</h3>
<textarea id="dict" autofocus>]] .. content .. [[</textarea>
<div class="buttons">
    <button class="cancel" onclick="cancel()">Cancel</button>
    <button class="save" onclick="save()">Save</button>
</div>
<script>
    function save() {
        window.location.href = 'hammerspoon://dict-save?data=' + encodeURIComponent(document.getElementById('dict').value);
    }
    function cancel() {
        window.location.href = 'hammerspoon://dict-cancel';
    }
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') { cancel(); }
        if (e.key === 's' && e.metaKey) { e.preventDefault(); save(); }
    });
</script>
</body>
</html>
]]

    dictWebview = hs.webview.new(rect)
    dictWebview:windowStyle({"titled", "closable", "resizable"})
    dictWebview:level(hs.canvas.windowLevels.floating)
    dictWebview:windowTitle("Whisper Dictionary")
    dictWebview:allowTextEntry(true)
    dictWebview:deleteOnClose(true)
    dictWebview:html(html)
    dictWebview:bringToFront()
    dictWebview:show()

    -- Handle native close button
    dictWebview:windowCallback(function(action)
        if action == "closing" then dictWebview = nil end
    end)
    end) -- end pcall
    if not ok then
        log("toggleDictionaryEditor ERROR: " .. tostring(err))
        hs.alert.show("Dictionary editor error: " .. tostring(err), 5)
    end
end

hs.hotkey.bind(config.hotkey_toggle_recording.mods, config.hotkey_toggle_recording.key, toggleRecording)
log("bound recording hotkey: " .. config.hotkey_toggle_recording.key)
hs.hotkey.bind(config.hotkey_dictionary_editor.mods, config.hotkey_dictionary_editor.key, toggleDictionaryEditor)
log("bound dictionary hotkey: " .. config.hotkey_dictionary_editor.key)

hs.shutdownCallback = function()
    stopWhisperServer()
    restoreDuckState()
end

updateState("idle")
hs.alert.show("Whisper recording ready (Cmd+Alt+R)")
