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
    border_flash_enabled = true,
    border_color_recording = "#ff2828",
    border_color_transcribing = "#46aaff",
    border_color_complete = "#28dc28",
    server_idle_timeout = 300,
    hotkey_toggle_recording = { mods = {"cmd", "alt"}, key = "R" },
    hotkey_dictionary_editor = { mods = {"cmd", "alt"}, key = "C" },
    hotkey_open_transcripts = { mods = {"cmd", "alt"}, key = "T" },
    hotkey_toggle_ducking = { mods = {"cmd", "alt"}, key = "Z" },
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
local ffmpegPath
local lastResolvedAudioDeviceName = nil

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

local function normalizeColorHex(value, fallback)
    local text = trim(value)
    if type(text) == "string" then
        text = text:lower()
        if text:match("^#[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]$") then
            return text
        end
    end
    return fallback
end

local function colorFromHex(hex)
    local normalized = normalizeColorHex(hex, "#ff2828")
    return {
        red = tonumber(normalized:sub(2, 3), 16) / 255,
        green = tonumber(normalized:sub(4, 5), 16) / 255,
        blue = tonumber(normalized:sub(6, 7), 16) / 255,
    }
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

local function configuredAudioDeviceMatches(device, query)
    if not device or not query then return false end

    local uid = device:uid()
    if uid and uid == query then
        return true
    end

    local name = device:name()
    if name == query then
        return true
    end

    local queryLower = query:lower()
    return name and name:lower():find(queryLower, 1, true) ~= nil
end

local function resolveConfiguredAudioInputDevice()
    local query = configuredAudioDeviceQuery()
    if not query then return nil end

    local partialMatch = nil
    for _, device in ipairs(hs.audiodevice.allInputDevices() or {}) do
        if configuredAudioDeviceMatches(device, query) then
            local uid = device:uid()
            if uid == query or device:name() == query then
                return device
            end
            if not partialMatch then
                partialMatch = device
            end
        end
    end

    return partialMatch
end

local function listAvfoundationAudioInputs()
    if not ffmpegPath or not pathExists(ffmpegPath) then
        return nil, "ffmpeg not found"
    end

    local probe = shellQuote(ffmpegPath) .. " -hide_banner -f avfoundation -list_devices true -i '' 2>&1"
    local output = hs.execute("/bin/sh -c " .. shellQuote(probe), true) or ""
    local inputs = {}
    local inAudioSection = false

    for rawLine in output:gmatch("[^\r\n]+") do
        local line = trim(rawLine) or ""
        if line:find("AVFoundation audio devices:", 1, true) then
            inAudioSection = true
        elseif line:find("AVFoundation video devices:", 1, true) then
            inAudioSection = false
        elseif inAudioSection then
            local index, name = line:match("%[(%d+)%]%s+(.+)$")
            if index and name then
                table.insert(inputs, {
                    index = index,
                    name = trim(name),
                })
            end
        end
    end

    if #inputs == 0 then
        return nil, "No AVFoundation audio devices reported by ffmpeg"
    end

    return inputs
end

local function avfoundationAudioInputSpecifierForName(name)
    local query = trim(name)
    if not query then return nil end

    local inputs, err = listAvfoundationAudioInputs()
    if not inputs then
        return nil, err
    end

    for _, input in ipairs(inputs) do
        if input.name == query then
            lastResolvedAudioDeviceName = input.name
            return input.index
        end
    end

    local queryLower = query:lower()
    local partialMatch = nil
    local partialMatchCount = 0
    for _, input in ipairs(inputs) do
        if input.name and input.name:lower():find(queryLower, 1, true) then
            partialMatch = input
            partialMatchCount = partialMatchCount + 1
        end
    end

    if partialMatchCount == 1 then
        lastResolvedAudioDeviceName = partialMatch.name
        return partialMatch.index
    end

    if partialMatchCount > 1 then
        return nil, "Audio input device name is ambiguous: " .. query
    end

    return nil, "Audio input device not available to ffmpeg: " .. query
end

local function avfoundationAudioInputSpecifier(device)
    if not device then
        lastResolvedAudioDeviceName = nil
        return "default"
    end
    return avfoundationAudioInputSpecifierForName(device:name())
end

local function selectedAudioDeviceValue(device)
    if not device then return nil end
    return device:name() or device:uid()
end

local function configuredAudioDeviceIsSelected(device)
    local query = configuredAudioDeviceQuery()
    if not query then return false end
    return configuredAudioDeviceMatches(device, query)
end

local function configuredAudioDeviceDisplayName()
    local device = resolveConfiguredAudioInputDevice()
    if device then
        return device:name()
    end

    return configuredAudioDeviceQuery()
end

local function recordingAudioInputSpecifier()
    local query = configuredAudioDeviceQuery()
    local device = resolveConfiguredAudioInputDevice()
    if query and not device then
        return avfoundationAudioInputSpecifierForName(query)
    end

    local specifier, err = avfoundationAudioInputSpecifier(device)
    if not specifier then
        return nil, err
    end

    if device then
        lastResolvedAudioDeviceName = device:name()
    end

    return specifier
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
ffmpegPath = config.ffmpeg_path
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
lastResolvedAudioDeviceName = nil
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

local function pinConfiguredSystemInputDevice(showAlertOnFailure)
    if not config.pin_system_input_device then return true end

    local query = configuredAudioDeviceDisplayName()
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

local alertVerticalOffset = 35
local originalAlertShow = hs.alert.show

local function offsetAlertByUuid(uuid, yOffset)
    if not uuid or not yOffset or yOffset == 0 then return uuid end

    for _, alertEntry in ipairs(hs.alert._visibleAlerts or {}) do
        if alertEntry.UUID == uuid then
            for _, drawing in ipairs(alertEntry.drawings or {}) do
                local frame = drawing:frame()
                if frame then
                    drawing:setTopLeft({ x = frame.x, y = frame.y + yOffset })
                end
            end
            if alertEntry.frame then
                alertEntry.frame.y = alertEntry.frame.y + yOffset
            end
            break
        end
    end

    return uuid
end

hs.alert.show = function(...)
    hs.alert.closeAll(0)
    local uuid = originalAlertShow(...)
    return offsetAlertByUuid(uuid, alertVerticalOffset)
end

-- Border visual effects
local borderCanvas = nil
local borderFadeTimer = nil
local borderGeneration = 0

local borderColors = {
    recording = colorFromHex(config.border_color_recording),
    transcribing = colorFromHex(config.border_color_transcribing),
    complete = colorFromHex(config.border_color_complete),
}

local function clearBorder()
    if borderFadeTimer then borderFadeTimer:stop(); borderFadeTimer = nil end
    if borderCanvas then borderCanvas:delete(); borderCanvas = nil end
end

local function createBorder(color, alpha)
    if not config.border_flash_enabled then return nil end
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
    if not config.border_flash_enabled then return end
    local color = borderColors[colorName]
    local gen = createBorder(color, 0.9)
    if not gen then return end
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
local manualDuckEnabled = false
local reconcileDucking

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

reconcileDucking = function()
    if config.duck_enabled and (currentState == "recording" or manualDuckEnabled) then
        duckAudio()
    else
        unduckAudio()
    end
end

local function toggleManualDucking()
    if not config.duck_enabled then
        hs.alert.show("Ducking is disabled", 2)
        return
    end

    manualDuckEnabled = not manualDuckEnabled
    reconcileDucking()
    log("manual ducking toggled: " .. tostring(manualDuckEnabled))

    if manualDuckEnabled then
        hs.alert.show("Audio set to " .. tostring(config.duck_level) .. "%", 2)
    else
        hs.alert.show("Audio set to 100%", 2)
    end
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
    reconcileDucking()
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
    reconcileDucking()
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

    showBorder("recording")
    hs.alert.show("Recording started")

    recordingTask = hs.task.new(ffmpegPath, function(exitCode)
        log("ffmpeg exited: " .. tostring(exitCode) .. " session=" .. gen)
        if gen ~= sessionId then log("ffmpeg callback: stale session, ignoring"); return end

        -- If we're still in recording state, ffmpeg crashed unexpectedly
        if currentState == "recording" then
            log("ffmpeg crashed unexpectedly during recording")
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
            reconcileDucking()
            flashBorder("complete")
            hs.alert.show("Copied to clipboard\n\n" .. preview, 5)
            recordingStartedAt = nil
            lastRecordingDurationSeconds = nil
            idleResetTimer = hs.timer.doAfter(3, function()
                if gen ~= sessionId then return end
                updateState("idle")
                reconcileDucking()
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
    flashBorder("transcribing")
    updateState("transcribing")
    reconcileDucking()
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
local persistSettings
local readFileRaw
local openPath

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
    local ok, err = persistSettings(params)
    if not ok then
        hs.alert.show(err, 5)
        return
    end
    if config.pin_system_input_device then
        pinConfiguredSystemInputDevice(false)
    end
    borderColors = {
        recording = colorFromHex(config.border_color_recording),
        transcribing = colorFromHex(config.border_color_transcribing),
        complete = colorFromHex(config.border_color_complete),
    }
    hs.alert.show("Settings saved (" .. count .. " dictionary entries)")
    closeDictEditor()
end)

hs.urlevent.bind("dict-cancel", function()
    log("urlevent dict-cancel received")
    closeDictEditor()
end)

hs.urlevent.bind("lvs-open-log", function()
    log("urlevent lvs-open-log received")
    openPath(logFile, true)
end)

hs.urlevent.bind("lvs-open-transcript", function(eventName, params)
    local path = params.path
    log("urlevent lvs-open-transcript received: " .. tostring(path))
    if not openPath(path, false) then
        hs.alert.show("Could not open transcript", 3)
    end
end)

hs.urlevent.bind("lvs-copy-transcript", function(eventName, params)
    local path = params.path
    log("urlevent lvs-copy-transcript received: " .. tostring(path))
    local content = readFileRaw(path)
    if content then
        hs.pasteboard.setContents(content)
        hs.alert.show("Transcript copied", 2)
    else
        hs.alert.show("Could not copy transcript", 3)
    end
end)

hs.urlevent.bind("lvs-reload", function()
    log("urlevent lvs-reload received")
    closeDictEditor()
    hs.reload()
end)

local function readDictionaryRaw()
    local file = io.open(dictionaryFile, "r")
    if not file then return "" end
    local content = file:read("*a")
    file:close()
    return content
end

readFileRaw = function(path)
    if type(path) ~= "string" or path == "" then return nil end
    local file = io.open(path, "r")
    if not file then return nil end
    local content = file:read("*a")
    file:close()
    return content
end

local function luaStringLiteral(value)
    return string.format("%q", tostring(value))
end

local function isArrayTable(value)
    if type(value) ~= "table" then return false end
    local count = 0
    for key in pairs(value) do
        if type(key) ~= "number" or key < 1 or math.floor(key) ~= key then
            return false
        end
        count = count + 1
    end
    for i = 1, count do
        if value[i] == nil then return false end
    end
    return true
end

local function serializeLuaValue(value)
    if value == nil then return "nil" end

    local valueType = type(value)
    if valueType == "string" then
        return luaStringLiteral(value)
    end
    if valueType == "number" or valueType == "boolean" then
        return tostring(value)
    end
    if valueType == "table" then
        if isArrayTable(value) then
            local pieces = {}
            for _, item in ipairs(value) do
                table.insert(pieces, serializeLuaValue(item))
            end
            return "{" .. table.concat(pieces, ", ") .. "}"
        end

        local keys = {}
        for key in pairs(value) do
            if type(key) ~= "string" then
                error("Unsupported config table key type: " .. type(key))
            end
            table.insert(keys, key)
        end
        table.sort(keys)

        local pieces = {}
        for _, key in ipairs(keys) do
            table.insert(pieces, key .. " = " .. serializeLuaValue(value[key]))
        end
        return "{ " .. table.concat(pieces, ", ") .. " }"
    end

    error("Unsupported config value type: " .. valueType)
end

local function normalizedDuckLevel(selection)
    local value = tonumber(trim(selection or ""))
    if not value then
        return nil, "Ducking level must be a number between 0 and 100"
    end

    value = math.floor(value + 0.5)
    if value < 0 then value = 0 end
    if value > 100 then value = 100 end
    return value
end

local function normalizedTimeout(selection)
    local value = tonumber(trim(selection or ""))
    if not value then
        return nil, "Server idle timeout must be a number between 30 and 3600"
    end

    value = math.floor(value + 0.5)
    if value < 30 then value = 30 end
    if value > 3600 then value = 3600 end
    return value
end

local function normalizedBoolean(selection)
    local value = trim(selection or "")
    return value == "true" or value == "1" or value == "on"
end

local function hotkeyToString(hotkey)
    if type(hotkey) ~= "table" then return "" end
    local parts = {}
    for _, mod in ipairs(hotkey.mods or {}) do
        table.insert(parts, tostring(mod):lower())
    end
    if hotkey.key then
        table.insert(parts, tostring(hotkey.key):lower())
    end
    return table.concat(parts, "+")
end

local function normalizedHotkey(selection, fallback)
    local text = trim(selection)
    if not text or text == "" then
        return fallback
    end

    local mods = {}
    local seenMods = {}
    local finalKey = nil
    for rawToken in text:lower():gsub("<", ""):gsub(">", ""):gmatch("[^+]+") do
        local token = trim(rawToken)
        if token and token ~= "" then
            local normalized = token
            if token == "command" or token == "cmd" or token == "super" or token == "meta" then
                normalized = "cmd"
            elseif token == "option" or token == "alt" then
                normalized = "alt"
            elseif token == "control" or token == "ctrl" then
                normalized = "ctrl"
            elseif token == "shift" then
                normalized = "shift"
            end

            if normalized == "cmd" or normalized == "alt" or normalized == "ctrl" or normalized == "shift" then
                if not seenMods[normalized] then
                    table.insert(mods, normalized)
                    seenMods[normalized] = true
                end
            else
                if finalKey then
                    return nil, "Hotkeys must contain exactly one non-modifier key"
                end
                finalKey = normalized
            end
        end
    end

    if not finalKey then
        return nil, "Hotkeys must include a key"
    end
    if #mods == 0 then
        return nil, "Hotkeys must include at least one modifier"
    end

    return { mods = mods, key = finalKey:upper() }
end

local function persistConfigValues(updates)
    local normalized = {}
    local requestedKeys = {}
    for key, value in pairs(updates or {}) do
        normalized[key] = value
        requestedKeys[key] = true
    end

    local existing = readFileRaw(configFile)
    if not existing or existing == "" then
        local hasValues = false
        for _, value in pairs(normalized) do
            if value ~= nil then
                hasValues = true
                break
            end
        end
        if not hasValues then
            for key, value in pairs(normalized) do
                config[key] = value
            end
            lastResolvedAudioDeviceName = nil
            return true
        end

        local file, err = io.open(configFile, "w")
        if not file then
            return false, "Could not open config.lua for writing: " .. tostring(err)
        end
        file:write("return {\n")
        local keysToWrite = {}
        for key in pairs(requestedKeys) do
            table.insert(keysToWrite, key)
        end
        table.sort(keysToWrite)
        for _, key in ipairs(keysToWrite) do
            file:write("    " .. key .. " = " .. serializeLuaValue(normalized[key]) .. ",\n")
        end
        file:write("}\n")
        file:close()
        for key, value in pairs(normalized) do
            config[key] = value
        end
        lastResolvedAudioDeviceName = nil
        return true
    end

    local lines = {}
    for line in (existing .. "\n"):gmatch("([^\n]*)\n") do
        table.insert(lines, line)
    end

    local replaced = {}
    local closingIndex = nil
    for i, line in ipairs(lines) do
        local key = line:match("^%s*([%a_][%w_]*)%s*=")
        if key and requestedKeys[key] then
            local indent = line:match("^(%s*)") or "    "
            lines[i] = indent .. key .. " = " .. serializeLuaValue(normalized[key]) .. ","
            replaced[key] = true
        end
        if line:match("^%s*}%s*$") then
            closingIndex = i
        end
    end

    if not closingIndex then
        return false, "Could not update config.lua automatically"
    end

    local keysToInsert = {}
    for key in pairs(requestedKeys) do
        if not replaced[key] then
            table.insert(keysToInsert, key)
        end
    end
    table.sort(keysToInsert)
    for _, key in ipairs(keysToInsert) do
        table.insert(lines, closingIndex, "    " .. key .. " = " .. serializeLuaValue(normalized[key]) .. ",")
        closingIndex = closingIndex + 1
    end

    local file, err = io.open(configFile, "w")
    if not file then
        return false, "Could not open config.lua for writing: " .. tostring(err)
    end
    file:write(table.concat(lines, "\n"))
    file:close()

    for key, value in pairs(normalized) do
        config[key] = value
    end
    lastResolvedAudioDeviceName = nil
    return true
end

persistAudioDeviceSelection = function(selection)
    local normalized = trim(selection)
    if normalized == "" then normalized = nil end
    return persistConfigValues({
        audio_device = normalized,
    })
end

persistSettings = function(params)
    local normalizedAudioDevice = trim(params.audio_device)
    if normalizedAudioDevice == "" then normalizedAudioDevice = nil end

    local duckLevel, duckErr = normalizedDuckLevel(params.duck_level)
    if not duckLevel then
        return false, duckErr
    end

    local timeout, timeoutErr = normalizedTimeout(params.server_idle_timeout)
    if not timeout then
        return false, timeoutErr
    end

    local recordHotkey, recordErr = normalizedHotkey(params.hotkey_toggle_recording, defaults.hotkey_toggle_recording)
    if not recordHotkey then return false, recordErr end
    local settingsHotkey, settingsErr = normalizedHotkey(params.hotkey_dictionary_editor, defaults.hotkey_dictionary_editor)
    if not settingsHotkey then return false, settingsErr end
    local transcriptsHotkey, transcriptsErr = normalizedHotkey(params.hotkey_open_transcripts, defaults.hotkey_open_transcripts)
    if not transcriptsHotkey then return false, transcriptsErr end
    local duckHotkey, duckHotkeyErr = normalizedHotkey(params.hotkey_toggle_ducking, defaults.hotkey_toggle_ducking)
    if not duckHotkey then return false, duckHotkeyErr end

    local recordingColor = normalizeColorHex(params.border_color_recording, defaults.border_color_recording)
    local transcribingColor = normalizeColorHex(params.border_color_transcribing, defaults.border_color_transcribing)
    local completeColor = normalizeColorHex(params.border_color_complete, defaults.border_color_complete)

    return persistConfigValues({
        audio_device = normalizedAudioDevice,
        border_flash_enabled = normalizedBoolean(params.border_flash_enabled),
        border_color_recording = recordingColor,
        border_color_transcribing = transcribingColor,
        border_color_complete = completeColor,
        duck_level = duckLevel,
        hotkey_toggle_recording = recordHotkey,
        hotkey_dictionary_editor = settingsHotkey,
        hotkey_open_transcripts = transcriptsHotkey,
        hotkey_toggle_ducking = duckHotkey,
        server_idle_timeout = timeout,
    })
end

local function audioDeviceOptionsJson()
    local options = {{
        name = "",
        label = "System Default",
        detail = "Use whatever macOS currently exposes as the default input",
    }}
    local seenNames = {}

    local currentDefault = hs.audiodevice.defaultInputDevice()
    for _, device in ipairs(hs.audiodevice.allInputDevices() or {}) do
        local name = device:name()
        if name then
            seenNames[name] = true
            local value = selectedAudioDeviceValue(device) or name
            local detail = {}
            if currentDefault and currentDefault:uid() == device:uid() then
                table.insert(detail, "Current macOS default")
            end
            if configuredAudioDeviceIsSelected(device) then
                table.insert(detail, "Current Voice Scribe override")
            end
            if device:uid() then
                table.insert(detail, "CoreAudio UID: " .. device:uid())
            end
            table.insert(options, {
                name = value,
                label = name,
                detail = table.concat(detail, " | "),
            })
        end
    end

    local avfoundationInputs = listAvfoundationAudioInputs()
    if avfoundationInputs then
        for _, input in ipairs(avfoundationInputs) do
            if input.name and not seenNames[input.name] then
                local detail = "AVFoundation input"
                if config.audio_device == input.name then
                    detail = detail .. " | Current Voice Scribe override"
                end
                table.insert(options, {
                    name = input.name,
                    label = input.name,
                    detail = detail,
                })
                seenNames[input.name] = true
            end
        end
    end

    return hs.json.encode(options)
end

local function transcriptTimestamp(path)
    local attrs = hs.fs.attributes(path)
    if attrs and attrs.modification then return attrs.modification end
    return 0
end

local function transcriptTitle(path)
    local name = path:match("([^/]+)$") or path
    local stamp, duration = name:match("^transcript_(.-)__dur%-(%d+s)%.txt$")
    if stamp then
        return stamp:gsub("_", " ") .. " (" .. duration .. ")"
    end
    return name
end

local function transcriptPreview(path)
    local content = readFileRaw(path)
    if not content then return "" end
    content = content:gsub("%s+", " ")
    content = trim(content) or ""
    if #content > 120 then
        return content:sub(1, 117) .. "..."
    end
    return content
end

local function recentTranscriptsJson()
    local ok = ensureDirectory(transcriptDir)
    if not ok then return "[]" end

    local entries = {}
    for file in hs.fs.dir(transcriptDir) do
        if file ~= "." and file ~= ".." and file:match("%.txt$") then
            local path = transcriptDir .. "/" .. file
            local attrs = hs.fs.attributes(path)
            if attrs and attrs.mode == "file" then
                table.insert(entries, {
                    path = path,
                    title = transcriptTitle(path),
                    preview = transcriptPreview(path),
                    modified = transcriptTimestamp(path),
                })
            end
        end
    end

    table.sort(entries, function(a, b)
        return (a.modified or 0) > (b.modified or 0)
    end)
    while #entries > 8 do
        table.remove(entries)
    end
    return hs.json.encode(entries)
end

openPath = function(path, reveal)
    if type(path) ~= "string" or path == "" then return false end
    local cmd
    if reveal then
        cmd = "/usr/bin/open -R " .. shellQuote(path)
    else
        cmd = "/usr/bin/open " .. shellQuote(path)
    end
    local _, success = hs.execute(cmd, true)
    return success
end

local function escapeHtml(value)
    return tostring(value or "")
        :gsub("&", "&amp;")
        :gsub("<", "&lt;")
        :gsub(">", "&gt;")
        :gsub('"', "&quot;")
        :gsub("'", "&#39;")
end

local function toggleDictionaryEditor()
    log("toggleDictionaryEditor called")
    local ok, err = pcall(function()
    if dictWebview then
        closeDictEditor()
        return
    end

    local screen = hs.screen.mainScreen():frame()
    local w, h = 1040, 720
    local rect = hs.geometry.rect(screen.x + (screen.w - w) / 2, screen.y + (screen.h - h) / 2, w, h)

    local content = escapeHtml(readDictionaryRaw())
    local selectedAudioDevice = selectedAudioDeviceValue(resolveConfiguredAudioInputDevice()) or config.audio_device or ""
    local escapedSelectedAudioDevice = escapeHtml(selectedAudioDevice)
    local deviceOptionsJson = audioDeviceOptionsJson()
    local recentTranscripts = recentTranscriptsJson()
    local resolvedAudioDevice = configuredAudioDeviceDisplayName() or "System Default"
    local lastFfmpegInput = lastResolvedAudioDeviceName or "Not resolved yet"
    local borderEnabledChecked = config.border_flash_enabled and "checked" or ""

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
        width: 360px; padding: 12px; display: flex; flex-direction: column; gap: 12px;
        overflow-y: auto;
    }
    h3 { font-size: 13px; margin-bottom: 8px; color: #9da3af; font-weight: 600; }
    .panel {
        border-top: 1px solid #383838; padding-top: 12px; display: flex;
        flex-direction: column; gap: 8px;
    }
    .panel:first-child { border-top: none; padding-top: 0; }
    .subtle {
        color: #8b949e; font-size: 12px; line-height: 1.4;
    }
    textarea {
        flex: 1; background: #2d2d2d; color: #d4d4d4; border: 1px solid #444;
        border-radius: 4px; padding: 8px; font-size: 14px; font-family: 'SF Mono', Menlo, monospace;
        resize: none; outline: none;
    }
    textarea:focus { border-color: #666; }
    select, input[type="text"], input[type="number"], input[type="color"] {
        width: 100%; background: #2d2d2d; color: #d4d4d4; border: 1px solid #444;
        border-radius: 6px; padding: 8px; font-size: 13px; outline: none;
    }
    input[type="color"] { height: 36px; padding: 3px; }
    select:focus, input:focus { border-color: #666; }
    .checkbox-row { display: flex; gap: 8px; align-items: center; color: #c9d1d9; font-size: 12px; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .device-note {
        min-height: 38px; background: #1f1f1f; border-radius: 6px; padding: 8px;
        color: #a9b1ba; font-size: 12px; line-height: 1.4;
    }
    .field-group {
        display: flex; flex-direction: column; gap: 6px;
    }
    .field-label {
        color: #c9d1d9; font-size: 12px; font-weight: 600;
    }
    .status-list {
        background: #1f1f1f; border-radius: 6px; padding: 8px; display: grid;
        gap: 5px; color: #a9b1ba; font-size: 12px; line-height: 1.35;
    }
    .status-list strong { color: #d4d4d4; font-weight: 600; }
    .tool-row { display: flex; gap: 8px; flex-wrap: wrap; }
    .transcript-list { display: flex; flex-direction: column; gap: 8px; }
    .transcript-item { background: #1f1f1f; border-radius: 6px; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
    .transcript-title { color: #d4d4d4; font-size: 12px; font-weight: 600; }
    .transcript-preview { color: #8b949e; font-size: 12px; line-height: 1.35; }
    .mini-buttons { display: flex; gap: 6px; }
    .mini-buttons button, .tool-row button { padding: 5px 9px; font-size: 12px; }
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
        <div class="panel">
            <h3>Input Device</h3>
            <div class="subtle">Choose which microphone Voice Scribe should record from.</div>
            <select id="audio-device"></select>
            <div id="device-note" class="device-note"></div>
            <div class="status-list">
                <div><strong>Configured:</strong> ]] .. escapeHtml(resolvedAudioDevice) .. [[</div>
                <div><strong>Resolved by ffmpeg:</strong> ]] .. escapeHtml(lastFfmpegInput) .. [[</div>
            </div>
        </div>
        <div class="panel">
            <h3>Ducking</h3>
            <div class="subtle">Set how low Music and Spotify should drop while recording.</div>
            <div class="field-group">
                <label class="field-label" for="duck-level">Playback Level While Recording (%)</label>
                <input id="duck-level" type="number" min="0" max="100" step="1" value="]] .. tostring(config.duck_level or defaults.duck_level) .. [[" />
            </div>
            <div id="duck-note" class="device-note"></div>
        </div>
        <div class="panel">
            <h3>Hotkeys</h3>
            <div class="grid-2">
                <div class="field-group">
                    <label class="field-label" for="hotkey-record">Record</label>
                    <input id="hotkey-record" type="text" value="]] .. escapeHtml(hotkeyToString(config.hotkey_toggle_recording)) .. [[" />
                </div>
                <div class="field-group">
                    <label class="field-label" for="hotkey-settings">Settings</label>
                    <input id="hotkey-settings" type="text" value="]] .. escapeHtml(hotkeyToString(config.hotkey_dictionary_editor)) .. [[" />
                </div>
                <div class="field-group">
                    <label class="field-label" for="hotkey-transcripts">Transcripts</label>
                    <input id="hotkey-transcripts" type="text" value="]] .. escapeHtml(hotkeyToString(config.hotkey_open_transcripts)) .. [[" />
                </div>
                <div class="field-group">
                    <label class="field-label" for="hotkey-duck">Duck</label>
                    <input id="hotkey-duck" type="text" value="]] .. escapeHtml(hotkeyToString(config.hotkey_toggle_ducking)) .. [[" />
                </div>
            </div>
            <div class="subtle">Use forms like <code>cmd+alt+r</code> or <code>ctrl+shift+s</code>. Reload to apply changed hotkeys.</div>
        </div>
        <div class="panel">
            <h3>Border Flash</h3>
            <label class="checkbox-row"><input id="border-enabled" type="checkbox" ]] .. borderEnabledChecked .. [[ /> Enable status border</label>
            <div class="grid-2">
                <div class="field-group">
                    <label class="field-label" for="color-recording">Recording</label>
                    <input id="color-recording" type="color" value="]] .. escapeHtml(normalizeColorHex(config.border_color_recording, defaults.border_color_recording)) .. [[" />
                </div>
                <div class="field-group">
                    <label class="field-label" for="color-transcribing">Transcribing</label>
                    <input id="color-transcribing" type="color" value="]] .. escapeHtml(normalizeColorHex(config.border_color_transcribing, defaults.border_color_transcribing)) .. [[" />
                </div>
                <div class="field-group">
                    <label class="field-label" for="color-complete">Complete</label>
                    <input id="color-complete" type="color" value="]] .. escapeHtml(normalizeColorHex(config.border_color_complete, defaults.border_color_complete)) .. [[" />
                </div>
            </div>
        </div>
        <div class="panel">
            <h3>Daemon</h3>
            <div class="field-group">
                <label class="field-label" for="server-timeout">Server Idle Timeout (sec)</label>
                <input id="server-timeout" type="number" min="30" max="3600" step="30" value="]] .. tostring(config.server_idle_timeout or defaults.server_idle_timeout) .. [[" />
            </div>
            <div class="tool-row">
                <button class="cancel" onclick="openLog()">Open Log</button>
                <button class="cancel" onclick="reloadApp()">Reload</button>
            </div>
        </div>
        <div class="panel">
            <h3>Recent Transcripts</h3>
            <div id="transcript-list" class="transcript-list"></div>
        </div>
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
    const duckLevelInput = document.getElementById('duck-level');
    const duckNote = document.getElementById('duck-note');
    const selectedAudioDevice = "]] .. escapedSelectedAudioDevice .. [[";
    const recentTranscripts = ]] .. recentTranscripts .. [[;

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

    function updateDuckNote() {
        const raw = Number.parseInt(duckLevelInput.value, 10);
        const level = Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : 0;
        duckNote.textContent = 'Playback will be reduced to ' + level + '% of the current app volume while recording.';
    }

    duckLevelInput.addEventListener('input', updateDuckNote);
    updateDuckNote();

    function sendAction(name, params = {}) {
        const query = new URLSearchParams(params);
        window.location.href = 'hammerspoon://' + name + (query.toString() ? '?' + query.toString() : '');
    }

    function openLog() {
        sendAction('lvs-open-log');
    }

    function reloadApp() {
        sendAction('lvs-reload');
    }

    function renderTranscripts() {
        const container = document.getElementById('transcript-list');
        container.innerHTML = '';
        if (!recentTranscripts.length) {
            const empty = document.createElement('div');
            empty.className = 'device-note';
            empty.textContent = 'No archived transcripts yet.';
            container.appendChild(empty);
            return;
        }
        for (const transcript of recentTranscripts) {
            const item = document.createElement('div');
            item.className = 'transcript-item';

            const title = document.createElement('div');
            title.className = 'transcript-title';
            title.textContent = transcript.title || transcript.path;
            item.appendChild(title);

            const preview = document.createElement('div');
            preview.className = 'transcript-preview';
            preview.textContent = transcript.preview || 'Empty transcript';
            item.appendChild(preview);

            const buttons = document.createElement('div');
            buttons.className = 'mini-buttons';

            const openButton = document.createElement('button');
            openButton.className = 'cancel';
            openButton.textContent = 'Open';
            openButton.onclick = () => sendAction('lvs-open-transcript', { path: transcript.path });
            buttons.appendChild(openButton);

            const copyButton = document.createElement('button');
            copyButton.className = 'cancel';
            copyButton.textContent = 'Copy';
            copyButton.onclick = () => sendAction('lvs-copy-transcript', { path: transcript.path });
            buttons.appendChild(copyButton);

            item.appendChild(buttons);
            container.appendChild(item);
        }
    }
    renderTranscripts();

    function save() {
        const params = new URLSearchParams();
        params.set('data', document.getElementById('dict').value);
        params.set('audio_device', deviceSelect.value);
        params.set('duck_level', duckLevelInput.value);
        params.set('server_idle_timeout', document.getElementById('server-timeout').value);
        params.set('hotkey_toggle_recording', document.getElementById('hotkey-record').value);
        params.set('hotkey_dictionary_editor', document.getElementById('hotkey-settings').value);
        params.set('hotkey_open_transcripts', document.getElementById('hotkey-transcripts').value);
        params.set('hotkey_toggle_ducking', document.getElementById('hotkey-duck').value);
        params.set('border_flash_enabled', document.getElementById('border-enabled').checked ? 'true' : 'false');
        params.set('border_color_recording', document.getElementById('color-recording').value);
        params.set('border_color_transcribing', document.getElementById('color-transcribing').value);
        params.set('border_color_complete', document.getElementById('color-complete').value);
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
hs.hotkey.bind(config.hotkey_toggle_ducking.mods, config.hotkey_toggle_ducking.key, toggleManualDucking)
log("bound manual ducking hotkey: " .. config.hotkey_toggle_ducking.key)

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
