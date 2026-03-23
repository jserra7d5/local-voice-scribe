-- Voice Recording & Whisper Transcription Script
-- Hotkey: Cmd+Alt+R (toggle recording on/off)

local isRecording = false
local recordingTask = nil
local tempAudioFile = "/tmp/whisper_recording.wav"
local whisperServerPath = "/Users/joeserra/Documents/whisper.cpp/build/bin/whisper-server"
local modelPath = "/Users/joeserra/Documents/whisper.cpp/models/ggml-large-v3-turbo.bin"
local whisperServerPort = 8178
local whisperServerTimeout = 300
local stateFile = "/tmp/whisper_state.txt"
local duckStateFile = "/tmp/whisper_duck_state.txt"
local logFile = "/tmp/whisper_debug.log"
local currentState = "idle"
local duckLevel = 10

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
end

local function showBorder(colorName) createBorder(borderColors[colorName], 0.9) end

local function flashBorder(colorName)
    local color = borderColors[colorName]
    createBorder(color, 0.9)
    local steps = 11
    local step = 0
    borderFadeTimer = hs.timer.doEvery(0.05, function(timer)
        step = step + 1
        if step >= steps or not borderCanvas then
            timer:stop(); borderFadeTimer = nil; clearBorder(); return
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

local function rampVolume(appName, fromVol, toVol, duration)
    if duckRampTimers[appName] then duckRampTimers[appName]:stop(); duckRampTimers[appName] = nil end
    local steps = 10
    local interval = duration / steps
    local step = 0
    duckRampTimers[appName] = hs.timer.doEvery(interval, function(timer)
        step = step + 1
        setAppVolume(appName, fromVol + (toVol - fromVol) * (step / steps))
        if step >= steps then timer:stop(); duckRampTimers[appName] = nil end
    end)
end

local function duckAudio()
    savedVolumes = {}
    for _, appName in ipairs({"Music", "Spotify"}) do
        if isAppRunning(appName) then
            local vol = getAppVolume(appName)
            if vol and vol > 0 then
                savedVolumes[appName] = vol
                rampVolume(appName, vol, vol * (duckLevel / 100), 0.5)
            end
        end
    end
    if next(savedVolumes) then saveDuckState() end
end

local function unduckAudio()
    for appName, vol in pairs(savedVolumes) do
        if isAppRunning(appName) then
            rampVolume(appName, getAppVolume(appName) or (vol * (duckLevel / 100)), vol, 1.0)
        end
    end
    clearDuckState()
end

restoreDuckState()

-- Whisper server — dead simple approach
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
    whisperIdleTimer = hs.timer.doAfter(whisperServerTimeout, stopWhisperServer)
end

local function launchServerIfNeeded()
    if isServerUp() then
        log("server already up")
        resetServerIdleTimer()
        return
    end

    log("launching new server")
    hs.execute("lsof -ti:" .. whisperServerPort .. " | xargs kill -9 2>/dev/null", true)

    whisperServerTask = hs.task.new(whisperServerPath, function(exitCode)
        log("whisper-server exited: " .. tostring(exitCode))
        whisperServerTask = nil
    end, {
        "-m", modelPath,
        "-l", "en",
        "--port", tostring(whisperServerPort),
        "--host", "127.0.0.1",
    })
    whisperServerTask:start()
    log("server task started")
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

local function startRecording()
    isRecording = true
    updateState("recording")
    log("startRecording")

    if hs.fs.attributes(tempAudioFile) then os.remove(tempAudioFile) end

    duckAudio()
    showBorder("recording")
    hs.alert.show("Recording started")

    recordingTask = hs.task.new("/opt/homebrew/bin/ffmpeg", function(exitCode)
        log("ffmpeg exited: " .. tostring(exitCode))
    end, {
        "-y", "-f", "avfoundation", "-i", ":default",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        tempAudioFile
    })
    recordingTask:start()
    log("ffmpeg started pid=" .. tostring(recordingTask:pid()))
end

local function doTranscription()
    local attrs = hs.fs.attributes(tempAudioFile)
    log("doTranscription: file size=" .. tostring(attrs and attrs.size))

    if not attrs or attrs.size < 1000 then
        hs.alert.show("Recording too short", 3)
        updateState("idle")
        return
    end

    -- Wait for server, polling with actual HTTP check
    if not isServerUp() then
        log("server not up, waiting...")
        local attempts = 0
        hs.timer.doEvery(0.5, function(timer)
            attempts = attempts + 1
            if isServerUp() then
                timer:stop()
                log("server came up after " .. attempts .. " polls")
                doTranscription()
            elseif attempts > 20 then
                timer:stop()
                hs.alert.show("Server not available", 3)
                updateState("idle")
            end
        end)
        return
    end

    log("sending file to server...")
    local curlTask = hs.task.new("/usr/bin/curl", function(exitCode, stdOut, stdErr)
        log("curl exit=" .. tostring(exitCode))
        log("curl stdout=[" .. tostring(stdOut) .. "]")
        log("curl stderr=[" .. tostring(stdErr) .. "]")

        if exitCode ~= 0 or not stdOut or #stdOut == 0 then
            hs.alert.show("Transcription failed", 3)
            updateState("idle")
            return
        end

        local transcription = stdOut:match('"text"%s*:%s*"(.-)"')
        if transcription then
            transcription = transcription:gsub("\\n", " "):gsub("\\(.)", "%1")
            transcription = transcription:match("^%s*(.-)%s*$")
        end
        log("parsed transcription=[" .. tostring(transcription) .. "]")

        if transcription and #transcription > 0 then
            hs.pasteboard.setContents(transcription)
            local preview = transcription
            if #preview > 60 then preview = preview:sub(1, 60) end
            updateState("complete")
            flashBorder("complete")
            hs.alert.show("Copied to clipboard\n\n" .. preview, 5)
            hs.timer.doAfter(3, function() updateState("idle") end)
        else
            hs.alert.show("No transcription found", 3)
            updateState("idle")
        end

        if hs.fs.attributes(tempAudioFile) then os.remove(tempAudioFile) end
        resetServerIdleTimer()
    end, {
        "-s", "--max-time", "30",
        "-X", "POST",
        "-F", "file=@" .. tempAudioFile,
        "-F", "response_format=json",
        "http://127.0.0.1:" .. whisperServerPort .. "/inference",
    })
    curlTask:start()
    log("curl started")
end

local function stopRecording()
    isRecording = false
    log("stopRecording")

    if recordingTask then
        local pid = recordingTask:pid()
        log("sending SIGINT to ffmpeg pid=" .. tostring(pid))
        if pid then
            hs.execute("kill -INT " .. pid, true)
        end
        recordingTask = nil
    end

    unduckAudio()
    flashBorder("transcribing")
    updateState("transcribing")
    hs.alert.show("Recording stopped. Transcribing")

    -- Give ffmpeg time to finalize wav
    hs.timer.doAfter(0.7, doTranscription)
end

function toggleRecording()
    if isRecording then
        stopRecording()
    else
        startRecording()
    end
end

hs.hotkey.bind({"cmd", "alt"}, "R", toggleRecording)

hs.shutdownCallback = function()
    stopWhisperServer()
    restoreDuckState()
end

updateState("idle")
hs.alert.show("Whisper recording ready (Cmd+Alt+R)")
