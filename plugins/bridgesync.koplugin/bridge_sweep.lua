--[[--
Full-library highlight sweep.

Exchanges highlights with the bridge for EVERY history book that has a sidecar
— the first-sync backfill for a device's whole highlight back-catalog, and the
way web-side highlights reach books that were never annotated on this device.

Manual-only; day-to-day changes ride the periodic sync and the on-close
snapshot. The sweep runs as small steps chained through UIManager:scheduleIn so
the UI stays responsive, and it is a module-level singleton holding no
ReaderUI references, so it survives document open/close. The queue position is
persisted and advances only after a book's exchange (including its ack round)
succeeds, so an interrupted or failed sweep resumes exactly where it stopped.
]]

local DocSettings = require("docsettings")
local UIManager = require("ui/uimanager")
local lfs = require("libs/libkoreader-lfs")

local BridgeAnnotations = require("bridge_annotations")

local STATE_KEY = "annotation_sweep"
local STEP_DELAY_SECONDS = 0.1

local BridgeSweep = {
    running = false,
    cancel_requested = false,
}

function BridgeSweep.isRunning()
    return BridgeSweep.running
end

function BridgeSweep.cancel()
    BridgeSweep.cancel_requested = true
end

local function currentReaderFile()
    local ok, ReaderUI = pcall(require, "apps/reader/readerui")
    if ok and ReaderUI and ReaderUI.instance and ReaderUI.instance.document then
        return ReaderUI.instance.document.file
    end
    return nil
end

-- Every history entry with a sidecar (no cap — this is the whole point).
local function buildQueue()
    local ok_hist, ReadHistory = pcall(require, "readhistory")
    local history = (ok_hist and ReadHistory and ReadHistory.hist) or {}
    local queue, seen = {}, {}
    for _, item in ipairs(history) do
        local file = type(item) == "table" and item.file or nil
        if type(file) == "string" and file ~= "" and not seen[file]
            and lfs.attributes(file, "mode") == "file"
            and DocSettings:hasSidecarFile(file) then
            seen[file] = true
            table.insert(queue, file)
        end
    end
    return queue
end

local function sweepOneBook(bridge, file)
    -- The open book's sidecar is stale while reading; the periodic sync and
    -- the on-close snapshot own it.
    if file == currentReaderFile() then
        return { skipped = true }
    end
    if lfs.attributes(file, "mode") ~= "file" then
        return { skipped = true }
    end

    local doc_settings = DocSettings:open(file)
    local hash = BridgeAnnotations.resolveBookHash(file, doc_settings)
    if not hash then
        return { skipped = true }
    end
    local annotations = doc_settings:readSetting("annotations") or {}

    local result, err
    local ok_call, pcall_err = pcall(function()
        result, err = BridgeAnnotations.exchangeBooks(bridge, {
            { file = file, hash = hash, annotations = annotations, live = false },
        })
    end)
    if not ok_call then
        return nil, tostring(pcall_err)
    end
    if not result then
        return nil, tostring(err or "exchange failed")
    end
    return result
end

-- Starts (or resumes) the sweep. `on_done(totals, message)` fires on
-- completion, cancellation, or failure-stop; `on_progress(index, total)` after
-- every successfully processed book.
function BridgeSweep.start(bridge, on_progress, on_done)
    if BridgeSweep.running then
        return false, "already running"
    end

    local saved = bridge.state:readSetting(STATE_KEY)
    local queue, index
    if type(saved) == "table" and type(saved.queue) == "table"
        and #saved.queue > 0 and tonumber(saved.index) then
        queue, index = saved.queue, tonumber(saved.index)
        bridge:logInfo("Highlight sweep: resuming at book", tostring(index), "of", tostring(#queue))
    else
        queue, index = buildQueue(), 1
    end
    if #queue == 0 or index > #queue then
        bridge.state:delSetting(STATE_KEY)
        bridge.state:flush()
        return false, "no books with highlights sidecars in history"
    end

    BridgeSweep.running = true
    BridgeSweep.cancel_requested = false

    local function persist(i)
        bridge.state:saveSetting(STATE_KEY, { queue = queue, index = i })
        bridge.state:flush()
    end
    persist(index)

    local totals = { books = 0, skipped = 0, uploaded = 0, applied = 0, deleted = 0 }

    local function finish(message, keep_state)
        BridgeSweep.running = false
        if not keep_state then
            bridge.state:delSetting(STATE_KEY)
            bridge.state:flush()
        end
        if on_done then
            on_done(totals, message)
        end
    end

    local function step(i)
        if BridgeSweep.cancel_requested then
            persist(i)
            finish("cancelled — will resume from here", true)
            return
        end
        if i > #queue then
            finish(nil, false)
            return
        end

        local result, err = sweepOneBook(bridge, queue[i])
        if result then
            if result.skipped then
                totals.skipped = totals.skipped + 1
            else
                totals.books = totals.books + 1
                totals.uploaded = totals.uploaded + (result.uploaded or 0)
                totals.applied = totals.applied + (result.applied or 0)
                totals.deleted = totals.deleted + (result.deleted or 0)
            end
            -- Ack-gated advance: the exchange (and its ack round) succeeded,
            -- so this book never needs revisiting on resume.
            persist(i + 1)
            if on_progress then
                on_progress(i, #queue)
            end
            UIManager:scheduleIn(STEP_DELAY_SECONDS, function()
                step(i + 1)
            end)
        else
            -- Stop (likely network); the persisted index resumes this book.
            bridge:logWarn("Highlight sweep stopped at", tostring(queue[i]), ":", tostring(err))
            persist(i)
            finish("stopped: " .. tostring(err) .. " — will resume from here", true)
        end
    end

    UIManager:scheduleIn(STEP_DELAY_SECONDS, function()
        step(index)
    end)
    return true
end

return BridgeSweep
