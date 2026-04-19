-- Voice Recording & Whisper Transcription Script
-- Hotkey: Cmd+Alt+R (toggle recording on/off)

-- Config directory
local configDir = os.getenv("HOME") .. "/.local-voice-scribe"
local configFile = configDir .. "/config.lua"
local dictionaryFile = configDir .. "/dictionary.txt"
local runtimeFile = configDir .. "/runtime.lua"
local logFile = "/tmp/whisper_debug.log"
local transcriptDir = "/tmp/local-voice-scribe-transcripts"

local defaults = {
    duck_enabled = true,
    duck_level = 10,
    duck_ramp_down = 0.5,
    duck_ramp_up = 1.0,
    audio_device = nil,
    pin_system_input_device = false,
    server_idle_timeout = 300,
    hotkey_toggle_recording = { mods = {"cmd", "alt"}, key = "R" },
    hotkey_dictionary_editor = { mods = {"cmd", "alt"}, key = "C" },
    hotkey_open_transcripts = { mods = {"cmd", "alt"}, key = "T" },
    ffmpeg_path = nil,
    whisper_server_path = nil,
    whisper_public_path = nil,
    model_path = nil,
    ggml_metal_path_resources = nil,
    install_token = nil,
    repo_root = nil,
}

hs.fs.mkdir(configDir)
local config = {}
for k, v in pairs(defaults) do config[k] = v end

local runtimeOwnedKeys = {
    ffmpeg_path = true,
    whisper_server_path = true,
    whisper_public_path = true,
    model_path = true,
    ggml_metal_path_resources = true,
    install_token = true,
    repo_root = true,
}

local log

local function mergeConfig(candidate, source)
    if type(candidate) ~= "table" then return end
    for k, v in pairs(candidate) do
        if source == "user" and runtimeOwnedKeys[k] then
            hs.alert.show("Ignoring installer-owned config key: " .. k, 5)
        else
            config[k] = v
        end
    end
end

if hs.fs.attributes(runtimeFile) then
    local ok, runtimeConfig = pcall(dofile, runtimeFile)
    if ok then
        mergeConfig(runtimeConfig, "runtime")
    else
        hs.alert.show("Runtime config error: " .. tostring(runtimeConfig), 10)
    end
end

if hs.fs.attributes(configFile) then
    local ok, userConfig = pcall(dofile, configFile)
    if ok then
        mergeConfig(userConfig, "user")
    else
        hs.alert.show("Config error: " .. tostring(userConfig), 10)
    end
end

-- Create empty dictionary if missing
if not hs.fs.attributes(dictionaryFile) then
    local f = io.open(dictionaryFile, "w")
    if f then f:close() end
end

local function trim(s)
    if type(s) ~= "string" then return nil end
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function shellQuote(s)
    return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local function resolveCommand(name)
    local cmd = "/bin/sh -c " .. shellQuote("PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin command -v " .. name .. " 2>/dev/null")
    local output = hs.execute(cmd, true)
    output = trim(output)
    if output and #output > 0 then return output end
    return nil
end

local function pathExists(path)
    return type(path) == "string" and hs.fs.attributes(path) ~= nil
end

local function firstExistingPath(candidates)
    for _, candidate in ipairs(candidates) do
        if pathExists(candidate) then return candidate end
    end
    return nil
end

local function parentDir(path)
    return path and path:match("^(.*)/[^/]+$") or nil
end

local function ensureDirectory(path)
    if pathExists(path) then return true end
    local ok, err = pcall(hs.fs.mkdir, path)
    if ok and pathExists(path) then return true end
    return false, err
end

local function configuredAudioDeviceQuery()
    if type(config.audio_device) ~= "string" then return nil end
    local query = trim(config.audio_device)
    if not query or query == "" then return nil end
    return query
end

local function resolveConfiguredAudioInputDevice()
    local query = configuredAudioDeviceQuery()
    if not query then return nil end

    local queryLower = query:lower()
    local partialMatch = nil
    for _, device in ipairs(hs.audiodevice.allInputDevices() or {}) do
        local name = device:name()
        if name == query then
            return device
        end
        if not partialMatch and name and name:lower():find(queryLower, 1, true) then
            partialMatch = device
        end
    end

    return partialMatch
end

local function transcriptFilename(startedAt, durationSeconds)
    local stamp = os.date("%Y-%m-%d_%H-%M-%S", math.floor(startedAt or hs.timer.secondsSinceEpoch()))
    local duration = math.max(1, math.floor(tonumber(durationSeconds) or 0))
    return string.format("transcript_%s__dur-%ss.txt", stamp, duration)
end

local function archiveTranscription(text, startedAt, durationSeconds)
    if type(text) ~= "string" or text == "" then
        return nil, "empty transcription"
    end

    local ok, err = ensureDirectory(transcriptDir)
    if not ok then
        return nil, "could not create transcript directory: " .. tostring(err)
    end

    local path = transcriptDir .. "/" .. transcriptFilename(startedAt, durationSeconds)
    local file, openErr = io.open(path, "w")
    if not file then
        return nil, "could not open transcript file: " .. tostring(openErr)
    end
    file:write(text)
    file:close()
    log("saved transcript file: " .. path)
    return path
end

local function openTranscriptFolder()
    local ok, err = ensureDirectory(transcriptDir)
    if not ok then
        log("failed to create transcript directory: " .. tostring(err))
        hs.alert.show("Could not create transcript folder", 3)
        return
    end

    local output, success = hs.execute("/usr/bin/open " .. shellQuote(transcriptDir), true)
    if not success then
        log("failed to open transcript folder: " .. tostring(output))
        hs.alert.show("Could not open transcript folder", 3)
    end
end

local recordingTask = nil
local tempAudioFile = "/tmp/whisper_recording.wav"
local ffmpegPath = config.ffmpeg_path
    or firstExistingPath({
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    })
    or resolveCommand("ffmpeg")
local legacyWhisperRoot = os.getenv("HOME") .. "/Documents/whisper.cpp"
local whisperServerPath = config.whisper_server_path
    or firstExistingPath({
        legacyWhisperRoot .. "/build/bin/whisper-server",
    })
    or resolveCommand("whisper-server")
local whisperPublicPath = config.whisper_public_path
    or firstExistingPath({
        configDir .. "/whisper/public",
        legacyWhisperRoot .. "/examples/server/public",
    })
local whisperLibPath = firstExistingPath({
    (function()
        local serverDir = parentDir(whisperServerPath)
        local rootDir = parentDir(serverDir)
        return rootDir and (rootDir .. "/lib") or nil
    end)(),
    configDir .. "/whisper/lib",
})
local modelPath = config.model_path
    or firstExistingPath({
        configDir .. "/models/ggml-large-v3-turbo.bin",
        legacyWhisperRoot .. "/models/ggml-large-v3-turbo.bin",
    })
local ggmlMetalPathResources = config.ggml_metal_path_resources
local whisperServerPort = 8178
local stateFile = "/tmp/whisper_state.txt"
local duckStateFile = "/tmp/whisper_duck_state.txt"
local currentState = "idle"
local installToken = config.install_token or "legacy"
local sessionId = 0
local transcriptionStartedFor = nil
local recordingStartedAt = nil
local lastRecordingDurationSeconds = nil
local lastResolvedAudioDeviceName = nil
local idleResetTimer = nil
local ffmpegSafetyTimer = nil
local serverPollTimer = nil

log = function(msg)
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

local function recordingAudioInputSpecifier()
    local query = configuredAudioDeviceQuery()
    if not query then
        lastResolvedAudioDeviceName = nil
        return "default"
    end

    local device = resolveConfiguredAudioInputDevice()
    if not device then
        return nil, "Audio input device not found: " .. query
    end

    lastResolvedAudioDeviceName = device:name()
    return lastResolvedAudioDeviceName
end

local function pinConfiguredSystemInputDevice(showAlertOnFailure)
    if not config.pin_system_input_device then return true end

    local query = configuredAudioDeviceQuery()
    if not query then return true end

    local device = resolveConfiguredAudioInputDevice()
    if not device then
        local err = "Pinned audio input device not found: " .. query
        log(err)
        if showAlertOnFailure then hs.alert.show(err, 5) end
        return false
    end

    local current = hs.audiodevice.defaultInputDevice()
    if current and current:uid() == device:uid() then
        lastResolvedAudioDeviceName = device:name()
        return true
    end

    if device:setDefaultInputDevice() then
        lastResolvedAudioDeviceName = device:name()
        log("set system default input device: " .. tostring(lastResolvedAudioDeviceName))
        return true
    end

    local err = "Could not select system input device: " .. tostring(device:name())
    log(err)
    if showAlertOnFailure then hs.alert.show(err, 5) end
    return false
end

local function handleSystemAudioDeviceEvent(event)
    if not config.pin_system_input_device then return end
    if event ~= "dev#" and event ~= "dIn " then return end

    hs.timer.doAfter(0.2, function()
        pinConfiguredSystemInputDevice(false)
    end)
end

local function ensureWhisperDependencies()
    if not pathExists(whisperServerPath) then
        log("whisper-server missing: " .. tostring(whisperServerPath))
        hs.alert.show("whisper-server not found. Run setup.sh.", 5)
        return false
    end
    if whisperPublicPath and not pathExists(whisperPublicPath) then
        log("whisper public assets missing: " .. tostring(whisperPublicPath))
        hs.alert.show("Whisper public assets missing. Run setup.sh.", 5)
        return false
    end
    if not pathExists(modelPath) then
        log("model missing: " .. tostring(modelPath))
        hs.alert.show("Whisper model missing. Run setup.sh.", 5)
        return false
    end
    return true
end

local function ensureFfmpegDependency()
    if not pathExists(ffmpegPath) then
        log("ffmpeg missing: " .. tostring(ffmpegPath))
        hs.alert.show("ffmpeg not found. Run setup.sh.", 5)
        return false
    end
    return true
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
    borderFadeTimer = hs.timer.doEvery(0.05, function()
        step = step + 1
        if step >= steps or gen ~= borderGeneration or not borderCanvas then
            if borderFadeTimer then borderFadeTimer:stop(); borderFadeTimer = nil end
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
    local remaining = {}
    for line in file:lines() do
        local app, vol = line:match("(.+)=(%d+)")
        if app and vol then
            if isAppRunning(app) then
                setAppVolume(app, tonumber(vol))
            else
                remaining[app] = tonumber(vol)
            end
        end
    end
    file:close()
    savedVolumes = remaining
    if next(savedVolumes) then saveDuckState() else clearDuckState() end
end

local duckRampTimers = {}

local function rampVolume(appName, fromVol, toVol, duration, onComplete)
    if duckRampTimers[appName] then duckRampTimers[appName]:stop(); duckRampTimers[appName] = nil end
    local steps = 10
    local interval = duration / steps
    local step = 0
    duckRampTimers[appName] = hs.timer.doEvery(interval, function()
        step = step + 1
        setAppVolume(appName, fromVol + (toVol - fromVol) * (step / steps))
        if step >= steps then
            setAppVolume(appName, toVol)
            if duckRampTimers[appName] then duckRampTimers[appName]:stop(); duckRampTimers[appName] = nil end
            if onComplete then onComplete() end
        end
    end)
end

local function duckAudio()
    if not config.duck_enabled then return end
    for _, appName in ipairs({"Music", "Spotify"}) do
        if isAppRunning(appName) then
            -- Preserve original volume if we already have it (e.g., unduck still ramping)
            local original = savedVolumes[appName] or getAppVolume(appName)
            if original and original > 0 then
                savedVolumes[appName] = original
                local current = getAppVolume(appName) or original
                rampVolume(appName, current, original * (config.duck_level / 100), config.duck_ramp_down)
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
    if not ensureWhisperDependencies() then return end
    if isServerUp() then
        log("server already up")
        resetServerIdleTimer()
        return
    end

    log("launching new server")
    hs.execute("lsof -ti:" .. whisperServerPort .. " | xargs kill -9 2>/dev/null", true)

    local envParts = {}
    if pathExists(ggmlMetalPathResources) then
        table.insert(envParts, "GGML_METAL_PATH_RESOURCES=" .. shellQuote(ggmlMetalPathResources))
    end
    if pathExists(whisperLibPath) then
        table.insert(envParts, "DYLD_LIBRARY_PATH=" .. shellQuote(whisperLibPath))
        table.insert(envParts, "DYLD_FALLBACK_LIBRARY_PATH=" .. shellQuote(whisperLibPath))
    end
    local envPrefix = (#envParts > 0) and (table.concat(envParts, " ") .. " ") or ""
    whisperServerTask = hs.task.new("/bin/sh", function(exitCode)
        log("whisper-server exited: " .. tostring(exitCode))
        whisperServerTask = nil
    end, {
        "-c", envPrefix
            .. shellQuote(whisperServerPath)
            .. " -m " .. shellQuote(modelPath)
            .. " -l en --port " .. tostring(whisperServerPort)
            .. " --host 127.0.0.1"
            .. (whisperPublicPath and (" --public " .. shellQuote(whisperPublicPath)) or "")
            .. " >>" .. shellQuote(logFile) .. " 2>&1"
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

httpServer = hs.httpserver.new(false, false)
httpServer:setPort(8989)
httpServer:setCallback(function(method, path, headers, body)
    if path == "/state" then
        local payload = hs.json.encode({
            state = currentState,
            install_token = installToken,
            repo_root = config.repo_root,
            audio_device = config.audio_device,
            pin_system_input_device = config.pin_system_input_device,
            resolved_audio_device = lastResolvedAudioDeviceName,
            ffmpeg_path = ffmpegPath,
            whisper_server_path = whisperServerPath,
            whisper_public_path = whisperPublicPath,
            model_path = modelPath,
        })
        return payload, 200, {["Content-Type"] = "application/json"}
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
    transcriptionStartedFor = nil
    recordingStartedAt = nil
    lastRecordingDurationSeconds = nil
    if message then hs.alert.show(message, 3) end
end

local function startTranscriptionOnce(gen)
    if gen ~= sessionId or transcriptionStartedFor == gen then return end
    transcriptionStartedFor = gen
    local ok, err = pcall(doTranscription, gen)
    if not ok then
        log("doTranscription ERROR: " .. tostring(err))
        finishTranscription("Transcription error: " .. tostring(err))
    end
end

local function startRecording()
    if not ensureFfmpegDependency() then return end
    if not ensureWhisperDependencies() then return end
    if not pinConfiguredSystemInputDevice(true) then return end

    local audioInputSpecifier, audioErr = recordingAudioInputSpecifier()
    if not audioInputSpecifier then
        log(audioErr)
        hs.alert.show(audioErr, 5)
        return
    end

    sessionId = sessionId + 1
    transcriptionStartedFor = nil
    lastRecordingDurationSeconds = nil
    local gen = sessionId
    updateState("recording")
    log("startRecording session=" .. gen)
    log("recording audio input=" .. tostring(lastResolvedAudioDeviceName or audioInputSpecifier))
    recordingStartedAt = hs.timer.secondsSinceEpoch()

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

    recordingTask = hs.task.new(ffmpegPath, function(exitCode)
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
            startTranscriptionOnce(gen)
        end
    end, {
        "-y", "-f", "avfoundation", "-i", ":" .. audioInputSpecifier,
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

        local payload = hs.json.decode(stdOut)
        local transcription = payload and payload.text
        if type(transcription) == "string" then
            transcription = transcription:match("^%s*(.-)%s*$")
        end
        log("parsed transcription=[" .. tostring(transcription) .. "]")

        if transcription and #transcription > 0 then
            transcription = applyReplacements(transcription)
            local transcriptPath, archiveErr = archiveTranscription(transcription, recordingStartedAt, lastRecordingDurationSeconds)
            if transcriptPath then
                log("archived transcript to " .. transcriptPath)
            else
                log("archiveTranscription failed: " .. tostring(archiveErr))
            end
            hs.pasteboard.setContents(transcription)
            local preview = transcription
            if #preview > 60 then preview = preview:sub(1, 60) end
            updateState("complete")
            flashBorder("complete")
            hs.alert.show("Copied to clipboard\n\n" .. preview, 5)
            recordingStartedAt = nil
            lastRecordingDurationSeconds = nil
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
    if recordingStartedAt then
        local elapsed = hs.timer.secondsSinceEpoch() - recordingStartedAt
        lastRecordingDurationSeconds = math.max(1, math.floor(elapsed + 0.5))
    else
        lastRecordingDurationSeconds = nil
    end

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
        startTranscriptionOnce(gen)
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
local persistAudioDeviceSelection

local function closeDictEditor()
    if dictWebview then dictWebview:delete(); dictWebview = nil end
end

hs.urlevent.bind("dict-save", function(eventName, params)
    log("urlevent dict-save received")
    local data = params.data or ""
    local count = 0
    for line in data:gmatch("[^\n]+") do
        if line:match("%S") then count = count + 1 end
    end
    local file = io.open(dictionaryFile, "w")
    if file then
        file:write(data)
        file:close()
    else
        hs.alert.show("Could not write dictionary", 5)
        return
    end
    local ok, err = persistAudioDeviceSelection(params.audio_device)
    if not ok then
        hs.alert.show(err, 5)
        return
    end
    if config.pin_system_input_device then
        pinConfiguredSystemInputDevice(false)
    end
    hs.alert.show("Settings saved (" .. count .. " dictionary entries)")
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

local function readFileRaw(path)
    local file = io.open(path, "r")
    if not file then return nil end
    local content = file:read("*a")
    file:close()
    return content
end

local function luaStringLiteral(value)
    return string.format("%q", tostring(value))
end

persistAudioDeviceSelection = function(selection)
    local normalized = trim(selection)
    if normalized == "" then normalized = nil end

    local existing = readFileRaw(configFile)
    if not existing or existing == "" then
        if not normalized then
            config.audio_device = nil
            lastResolvedAudioDeviceName = nil
            return true
        end

        local file, err = io.open(configFile, "w")
        if not file then
            return false, "Could not open config.lua for writing: " .. tostring(err)
        end
        file:write("return {\n    audio_device = " .. luaStringLiteral(normalized) .. ",\n}\n")
        file:close()
        config.audio_device = normalized
        lastResolvedAudioDeviceName = nil
        return true
    end

    local replacement = "    audio_device = " .. (normalized and luaStringLiteral(normalized) or "nil") .. ","
    local lines = {}
    for line in (existing .. "\n"):gmatch("([^\n]*)\n") do
        table.insert(lines, line)
    end

    local replaced = false
    local closingIndex = nil
    for i, line in ipairs(lines) do
        if line:match("^%s*audio_device%s*=") then
            local indent = line:match("^(%s*)") or "    "
            lines[i] = indent .. "audio_device = " .. (normalized and luaStringLiteral(normalized) or "nil") .. ","
            replaced = true
        end
        if line:match("^%s*}%s*$") then
            closingIndex = i
        end
    end

    if not replaced then
        if not closingIndex then
            return false, "Could not update config.lua automatically"
        end
        table.insert(lines, closingIndex, replacement)
    end

    local file, err = io.open(configFile, "w")
    if not file then
        return false, "Could not open config.lua for writing: " .. tostring(err)
    end
    file:write(table.concat(lines, "\n"))
    file:close()

    config.audio_device = normalized
    lastResolvedAudioDeviceName = nil
    return true
end

local function audioDeviceOptionsJson()
    local options = {{
        name = "",
        label = "System Default",
        detail = "Use whatever macOS currently exposes as the default input",
    }}

    local currentDefault = hs.audiodevice.defaultInputDevice()
    for _, device in ipairs(hs.audiodevice.allInputDevices() or {}) do
        local name = device:name()
        if name then
            local detail = {}
            if currentDefault and currentDefault:uid() == device:uid() then
                table.insert(detail, "Current macOS default")
            end
            if config.audio_device == name then
                table.insert(detail, "Current Voice Scribe override")
            end
            table.insert(options, {
                name = name,
                label = name,
                detail = table.concat(detail, " | "),
            })
        end
    end

    return hs.json.encode(options)
end

local function toggleDictionaryEditor()
    log("toggleDictionaryEditor called")
    local ok, err = pcall(function()
    if dictWebview then
        closeDictEditor()
        return
    end

    local screen = hs.screen.mainScreen():frame()
    local w, h = 760, 420
    local rect = hs.geometry.rect(screen.x + (screen.w - w) / 2, screen.y + (screen.h - h) / 2, w, h)

    local content = readDictionaryRaw():gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"):gsub('"', "&quot;"):gsub("'", "&#39;")
    local selectedAudioDevice = config.audio_device or ""
    local escapedSelectedAudioDevice = selectedAudioDevice:gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"):gsub('"', "&quot;"):gsub("'", "&#39;")
    local deviceOptionsJson = audioDeviceOptionsJson()

    local html = [[
<!DOCTYPE html>
<html>
<head>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        background: #1e1e1e; color: #d4d4d4; font-family: -apple-system, sans-serif;
        padding: 14px; display: flex; flex-direction: column; height: 100vh; gap: 12px;
    }
    .layout {
        flex: 1; display: flex; gap: 12px; min-height: 0;
    }
    .main, .sidebar {
        background: #252526; border: 1px solid #3a3a3a; border-radius: 8px;
    }
    .main {
        flex: 1; padding: 12px; display: flex; flex-direction: column; min-height: 0;
    }
    .sidebar {
        width: 250px; padding: 12px; display: flex; flex-direction: column; gap: 10px;
    }
    h3 { font-size: 13px; margin-bottom: 8px; color: #9da3af; font-weight: 600; }
    .subtle {
        color: #8b949e; font-size: 12px; line-height: 1.4;
    }
    textarea {
        flex: 1; background: #2d2d2d; color: #d4d4d4; border: 1px solid #444;
        border-radius: 4px; padding: 8px; font-size: 14px; font-family: 'SF Mono', Menlo, monospace;
        resize: none; outline: none;
    }
    textarea:focus { border-color: #666; }
    select {
        width: 100%; background: #2d2d2d; color: #d4d4d4; border: 1px solid #444;
        border-radius: 6px; padding: 8px; font-size: 13px; outline: none;
    }
    select:focus { border-color: #666; }
    .device-note {
        min-height: 38px; background: #1f1f1f; border-radius: 6px; padding: 8px;
        color: #a9b1ba; font-size: 12px; line-height: 1.4;
    }
    .buttons { display: flex; gap: 8px; justify-content: flex-end; }
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
<div class="layout">
    <div class="main">
        <h3>Whisper Dictionary</h3>
        <div class="subtle" style="margin-bottom: 8px;">One word per line, or <code>wrong -> right</code>.</div>
        <textarea id="dict" autofocus>]] .. content .. [[</textarea>
    </div>
    <div class="sidebar">
        <div>
            <h3>Input Device</h3>
            <div class="subtle">Choose which microphone Voice Scribe should record from.</div>
        </div>
        <select id="audio-device"></select>
        <div id="device-note" class="device-note"></div>
        <div class="subtle">This sets the app's recording input. It does not need to follow the active Bluetooth device.</div>
    </div>
</div>
<div class="buttons">
    <button class="cancel" onclick="cancel()">Cancel</button>
    <button class="save" onclick="save()">Save</button>
</div>
<script>
    const deviceOptions = ]] .. deviceOptionsJson .. [[;
    const deviceSelect = document.getElementById('audio-device');
    const deviceNote = document.getElementById('device-note');
    const selectedAudioDevice = "]] .. escapedSelectedAudioDevice .. [[";

    for (const option of deviceOptions) {
        const el = document.createElement('option');
        el.value = option.name;
        el.textContent = option.label;
        if (option.name === selectedAudioDevice) el.selected = true;
        deviceSelect.appendChild(el);
    }

    function updateDeviceNote() {
        const current = deviceOptions.find(option => option.name === deviceSelect.value);
        deviceNote.textContent = current && current.detail ? current.detail : 'Voice Scribe will use the selected input on the next recording.';
    }

    deviceSelect.addEventListener('change', updateDeviceNote);
    updateDeviceNote();

    function save() {
        const params = new URLSearchParams();
        params.set('data', document.getElementById('dict').value);
        params.set('audio_device', deviceSelect.value);
        window.location.href = 'hammerspoon://dict-save?' + params.toString();
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
    dictWebview:windowTitle("Whisper Dictionary & Input")
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
hs.hotkey.bind(config.hotkey_open_transcripts.mods, config.hotkey_open_transcripts.key, openTranscriptFolder)
log("bound transcript hotkey: " .. config.hotkey_open_transcripts.key)

if config.pin_system_input_device then
    hs.audiodevice.watcher.setCallback(handleSystemAudioDeviceEvent)
    hs.audiodevice.watcher.start()
    pinConfiguredSystemInputDevice(false)
    log("enabled system audio input pinning")
end

hs.shutdownCallback = function()
    hs.audiodevice.watcher.stop()
    stopWhisperServer()
    restoreDuckState()
end

updateState("idle")
if pathExists(ffmpegPath) and pathExists(whisperServerPath) and pathExists(modelPath) then
    hs.alert.show("Whisper recording ready (Cmd+Alt+R)")
else
    hs.alert.show("Whisper loaded; run setup.sh to finish install")
end
