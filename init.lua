-- Voice Recording & Whisper Transcription Script
-- Hotkey: Cmd+Alt+R (toggle recording on/off)

local isRecording = false
local recordingTask = nil
local tempAudioFile = "/tmp/whisper_recording.wav"
local whisperPath = "/Users/joeserra/Documents/whisper.cpp/build/bin/whisper-cli"
local modelPath = "/Users/joeserra/Documents/whisper.cpp/models/ggml-large-v3-turbo.bin"
local stateFile = "/tmp/whisper_state.txt"
local duckStateFile = "/tmp/whisper_duck_state.txt"
local currentState = "idle"
local duckLevel = 10 -- percentage to duck to (0-100)

-- Position alerts at the top of the screen
hs.alert.defaultStyle.atScreenEdge = 1 -- 1 = top

-- Border visual effects for recording states
local borderCanvas = nil
local borderFadeTimer = nil

local borderColors = {
    recording = { red = 1, green = 0.15, blue = 0.15 },     -- red
    transcribing = { red = 1, green = 0.8, blue = 0 },      -- yellow
    complete = { red = 0.15, green = 0.85, blue = 0.15 },   -- green
}

local function clearBorder()
    if borderFadeTimer then
        borderFadeTimer:stop()
        borderFadeTimer = nil
    end
    if borderCanvas then
        borderCanvas:delete()
        borderCanvas = nil
    end
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

    -- Top, Bottom, Left, Right
    borderCanvas:appendElements(
        { type = "rectangle", frame = { x = 0, y = 0, w = screen.w, h = thickness }, fillColor = fillColor, strokeColor = noStroke },
        { type = "rectangle", frame = { x = 0, y = screen.h - thickness, w = screen.w, h = thickness }, fillColor = fillColor, strokeColor = noStroke },
        { type = "rectangle", frame = { x = 0, y = 0, w = thickness, h = screen.h }, fillColor = fillColor, strokeColor = noStroke },
        { type = "rectangle", frame = { x = screen.w - thickness, y = 0, w = thickness, h = screen.h }, fillColor = fillColor, strokeColor = noStroke }
    )
    borderCanvas:show()
    return borderCanvas
end

-- Show a persistent border (stays until cleared)
local function showBorder(colorName)
    createBorder(borderColors[colorName], 0.9)
end

-- Flash a border that fades out
local function flashBorder(colorName)
    local color = borderColors[colorName]
    createBorder(color, 0.9)

    local steps = 11
    local step = 0
    borderFadeTimer = hs.timer.doEvery(0.05, function(timer)
        step = step + 1
        if step >= steps or not borderCanvas then
            timer:stop()
            borderFadeTimer = nil
            clearBorder()
            return
        end
        local alpha = 0.9 * (1 - step / steps)
        for i = 1, borderCanvas:elementCount() do
            borderCanvas:elementAttribute(i, "fillColor", {
                red = color.red, green = color.green, blue = color.blue, alpha = alpha
            })
        end
    end)
end

-- Audio ducking for Music and Spotify
local savedVolumes = {}

local function getAppVolume(appName)
    local ok, vol = hs.osascript.applescript(
        'tell application "' .. appName .. '" to get sound volume'
    )
    if ok then return vol end
    return nil
end

local function setAppVolume(appName, vol)
    hs.osascript.applescript(
        'tell application "' .. appName .. '" to set sound volume to ' .. math.floor(vol)
    )
end

local function isAppRunning(appName)
    return hs.application.get(appName) ~= nil
end

local function saveDuckState()
    local file = io.open(duckStateFile, "w")
    if file then
        for app, vol in pairs(savedVolumes) do
            file:write(app .. "=" .. vol .. "\n")
        end
        file:close()
    end
end

local function clearDuckState()
    os.remove(duckStateFile)
    savedVolumes = {}
end

local function restoreDuckState()
    local file = io.open(duckStateFile, "r")
    if not file then return end
    for line in file:lines() do
        local app, vol = line:match("(.+)=(%d+)")
        if app and vol and isAppRunning(app) then
            setAppVolume(app, tonumber(vol))
        end
    end
    file:close()
    clearDuckState()
end

local duckRampTimers = {}

local function rampVolume(appName, fromVol, toVol, duration, callback)
    if duckRampTimers[appName] then
        duckRampTimers[appName]:stop()
        duckRampTimers[appName] = nil
    end
    local steps = 10
    local interval = duration / steps
    local step = 0
    duckRampTimers[appName] = hs.timer.doEvery(interval, function(timer)
        step = step + 1
        local t = step / steps
        local vol = fromVol + (toVol - fromVol) * t
        setAppVolume(appName, vol)
        if step >= steps then
            timer:stop()
            duckRampTimers[appName] = nil
            if callback then callback() end
        end
    end)
end

local function duckAudio()
    savedVolumes = {}
    local apps = {"Music", "Spotify"}
    for _, appName in ipairs(apps) do
        if isAppRunning(appName) then
            local vol = getAppVolume(appName)
            if vol and vol > 0 then
                savedVolumes[appName] = vol
                rampVolume(appName, vol, vol * (duckLevel / 100), 0.5)
            end
        end
    end
    if next(savedVolumes) then
        saveDuckState()
    end
end

local function unduckAudio()
    for appName, vol in pairs(savedVolumes) do
        if isAppRunning(appName) then
            local currentVol = getAppVolume(appName) or (vol * (duckLevel / 100))
            rampVolume(appName, currentVol, vol, 1.0)
        end
    end
    clearDuckState()
end

-- Restore volumes on Hammerspoon reload in case we crashed while ducked
restoreDuckState()

local function updateState(state)
    currentState = state
    local file = io.open(stateFile, "w")
    if file then
        file:write(state)
        file:close()
    end
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

    if hs.fs.attributes(tempAudioFile) then
        os.remove(tempAudioFile)
    end

    duckAudio()
    showBorder("recording")
    hs.alert.show("Recording started")
    
    recordingTask = hs.task.new("/opt/homebrew/bin/ffmpeg", function(exitCode, stdOut, stdErr)
    end, {
        "-f", "avfoundation",
        "-i", ":default",
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        tempAudioFile
    })
    
    recordingTask:start()
end

local function stopRecording()
    isRecording = false

    if recordingTask then
        recordingTask:terminate()
        recordingTask = nil
    end

    unduckAudio()
    flashBorder("transcribing")
    updateState("transcribing")
    hs.alert.show("Recording stopped. Transcribing")
    
    hs.timer.doAfter(0.5, function()
        local whisperTask = hs.task.new(whisperPath, function(exitCode, stdOut, stdErr)
            if exitCode == 0 then
                local transcription = ""
                for line in stdOut:gmatch("[^\r\n]+") do
                    local text = line:match("%[%d%d:%d%d:%d%d%.%d%d%d %-%-> %d%d:%d%d:%d%d%.%d%d%d%]%s+(.+)")
                    if text then
                        if transcription ~= "" then
                            transcription = transcription .. " "
                        end
                        transcription = transcription .. text
                    end
                end
                
                if transcription ~= "" then
                    hs.pasteboard.setContents(transcription)
                    local preview = transcription
                    if #preview > 60 then
                        preview = preview:sub(1, 60)
                    end
                    updateState("complete")
                    flashBorder("complete")
                    hs.alert.show("Copied to clipboard\n\n" .. preview, 5)
                    hs.timer.doAfter(3, function()
                        updateState("idle")
                    end)
                else
                    hs.alert.show("No transcription found", 3)
                    updateState("idle")
                end
            else
                hs.alert.show("Transcription failed", 3)
                updateState("idle")
            end
            
            if hs.fs.attributes(tempAudioFile) then
                os.remove(tempAudioFile)
            end
        end, {
            "-m", modelPath,
            "-f", tempAudioFile
        })
        
        whisperTask:start()
    end)
end

function toggleRecording()
    if isRecording then
        stopRecording()
    else
        startRecording()
    end
end

hs.hotkey.bind({"cmd", "alt"}, "R", toggleRecording)

updateState("idle")
hs.alert.show("Whisper recording ready (Cmd+Alt+R to toggle)")
