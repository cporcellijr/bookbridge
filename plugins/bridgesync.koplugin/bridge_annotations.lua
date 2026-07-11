--[[--
Two-way highlight/note sync with the bridge's annotation hub.

Each sync round, for recently-read books: upload this device's highlight
changes (new/edited since the per-book watermark) together with the complete
key list (the bridge detects deletions from keys that disappear), apply the
bridge's pending changes from other devices / BookOrbit's web reader into the
book's sidecar, and acknowledge what landed.

Positions are KOReader xpointers; only string positions sync (EPUB/rolling
documents — PDF position tables are skipped). Books currently open in the
reader are upload-only: server-side changes for them are deferred to the next
sync after the book is closed, where the sidecar merge path (annotations +
annotations_externally_modified, the same contract KOHighlights uses) lets
KOReader re-validate and re-sort on next open.
]]

local DocSettings = require("docsettings")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local md5 = require("ffi/sha2").md5

local MAX_BOOKS_PER_EXCHANGE = 20
local MAX_CHANGES_PER_BOOK = 50
local MAX_HISTORY_BOOKS = 30
local MAX_KEYS_PER_BOOK = 5000
local MAX_PULL_ROUNDS = 10
local MAX_FIELD_LENGTH = {
    color = 30,
    text = 10000,
    note = 5000,
    chapter = 500,
    pos = 4000,
}

local BridgeAnnotations = {}

local function deviceNow()
    return os.date("%Y-%m-%d %H:%M:%S")
end

-- Canonicalize an xpointer so the identity key survives crengine's
-- re-serialization (a trailing `.0` text offset and `[1]` sibling indexes come
-- and go between the authoring device and a receiving device for the SAME
-- highlight). MUST match the bridge's _normalize_xpointer_for_key. The raw
-- pos0 is still sent separately for positioning — this only feeds the key.
function BridgeAnnotations.normalizeXPointer(pos0)
    if type(pos0) ~= "string" then return "" end
    pos0 = pos0:gsub("%[1%]", "")
    pos0 = pos0:gsub("%.0$", "")
    return pos0
end

function BridgeAnnotations.buildKey(datetime, pos0)
    return md5(tostring(datetime) .. "|" .. BridgeAnnotations.normalizeXPointer(pos0))
end

local function truncatedString(value, max_length)
    if type(value) ~= "string" or value == "" then return nil end
    if #value > max_length then return value:sub(1, max_length) end
    return value
end

-- Normalize a sidecar annotation entry into the wire format. Returns nil for
-- entries that can't sync (bookmarks without a range, PDF table positions).
function BridgeAnnotations.normalizeEntry(a)
    if type(a) ~= "table" then return nil end
    if type(a.datetime) ~= "string" or a.datetime == "" then return nil end
    local pos0 = truncatedString(a.pos0, MAX_FIELD_LENGTH.pos)
    local pos1 = truncatedString(a.pos1, MAX_FIELD_LENGTH.pos)
    if not pos0 or not pos1 then return nil end
    local entry = {
        datetime = a.datetime,
        drawer = type(a.drawer) == "string" and a.drawer or "lighten",
        posFormat = "xpointer",
        pos0 = pos0,
        pos1 = pos1,
    }
    if type(a.datetime_updated) == "string" and a.datetime_updated ~= "" then
        entry.datetimeUpdated = a.datetime_updated
    end
    entry.color = truncatedString(a.color, MAX_FIELD_LENGTH.color)
    entry.text = truncatedString(a.text, MAX_FIELD_LENGTH.text)
    entry.note = truncatedString(a.note, MAX_FIELD_LENGTH.note)
    entry.chapter = truncatedString(a.chapter, MAX_FIELD_LENGTH.chapter)
    if type(a.pageno) == "number" then entry.pageno = a.pageno end
    return entry
end

-- Server entries arrive as decoded JSON, where an absent optional field is a
-- null sentinel (a function/userdata in KOReader's json lib), NOT nil. Passing
-- that straight into a KOReader annotation crashes the bookmark list ("attempt
-- to concatenate field 'note' (a function value)"). Coerce every field to its
-- expected type or nil.
local function _s(v) return type(v) == "string" and v or nil end
local function _n(v) return type(v) == "number" and v or nil end

-- A server entry mapped back into KOReader's sidecar annotation shape.
local function toSidecarEntry(entry)
    return {
        datetime = _s(entry.datetime),
        datetime_updated = _s(entry.datetimeUpdated),
        drawer = _s(entry.drawer) or "lighten",
        color = _s(entry.color),
        text = _s(entry.text),
        note = _s(entry.note),
        chapter = _s(entry.chapter),
        pageno = _n(entry.pageno),
        page = _s(entry.pos0),
        pos0 = _s(entry.pos0),
        pos1 = _s(entry.pos1),
    }
end

local function entryTimestamp(entry)
    local updated = entry.datetimeUpdated or entry.datetime_updated
    if type(updated) == "string" and updated ~= "" then
        return updated
    end
    return entry.datetime or ""
end

-- ------------------------------------------------------------------
-- Book / annotation collection
-- ------------------------------------------------------------------

local function currentReaderFile()
    local ok, ReaderUI = pcall(require, "apps/reader/readerui")
    if not ok or not ReaderUI or not ReaderUI.instance then return nil, nil end
    local instance = ReaderUI.instance
    local file = instance.document and instance.document.file or nil
    local annotations = nil
    if instance.annotation and type(instance.annotation.annotations) == "table" then
        annotations = instance.annotation.annotations
    end
    return file, annotations
end

local function bookHash(file, doc_settings)
    local stored = doc_settings and doc_settings:readSetting("partial_md5_checksum")
    if type(stored) == "string" and #stored == 32 then
        return stored:lower()
    end
    local ok, util = pcall(require, "util")
    if ok and util and type(util.partialMD5) == "function" then
        local ok2, computed = pcall(util.partialMD5, file)
        if ok2 and type(computed) == "string" and #computed == 32 then
            return computed:lower()
        end
    end
    return nil
end

-- Resolve a book's KOSync hash (sidecar-stored or computed).
function BridgeAnnotations.resolveBookHash(file, doc_settings)
    if not doc_settings and DocSettings:hasSidecarFile(file) then
        doc_settings = DocSettings:open(file)
    end
    return bookHash(file, doc_settings)
end

-- Snapshot the live annotation list into plain tables while the ReaderUI is
-- still alive, so an upload after the document closes never touches reader
-- objects and never races the sidecar flush. Mirrors the capture/run split
-- convention: capture is synchronous, the upload runs later.
function BridgeAnnotations.captureLiveBook(ui)
    if not ui or not ui.document or type(ui.document.file) ~= "string" then
        return nil
    end
    local raw = ui.annotation and ui.annotation.annotations
    if type(raw) ~= "table" then
        return nil
    end

    local hash = nil
    if ui.doc_settings and ui.doc_settings.readSetting then
        local stored = ui.doc_settings:readSetting("partial_md5_checksum")
        if type(stored) == "string" and #stored == 32 then
            hash = stored:lower()
        end
    end

    local copies = {}
    for _, a in ipairs(raw) do
        if type(a) == "table" then
            table.insert(copies, {
                datetime = a.datetime,
                datetime_updated = a.datetime_updated,
                drawer = a.drawer,
                color = a.color,
                text = a.text,
                note = a.note,
                chapter = a.chapter,
                pageno = a.pageno,
                pos0 = a.pos0,
                pos1 = a.pos1,
            })
        end
    end

    -- live=false: by the time the deferred upload runs the book is closed, so
    -- server-side changes may be merged straight into the sidecar.
    return { file = ui.document.file, hash = hash, annotations = copies, live = false }
end

-- Read one closed book's sidecar after KOReader has had a chance to flush it.
-- Used by the close hook; unlike captureLiveBook this does not depend on the
-- reader UI still being alive.
function BridgeAnnotations.collectBookByFile(file, known_hash)
    if type(file) ~= "string" or file == "" or lfs.attributes(file, "mode") ~= "file" then
        return nil
    end
    if not DocSettings:hasSidecarFile(file) then
        return nil
    end
    local doc_settings = DocSettings:open(file)
    local hash = known_hash
    if type(hash) ~= "string" or #hash ~= 32 then
        hash = bookHash(file, doc_settings)
    end
    if type(hash) ~= "string" or #hash ~= 32 then
        return nil
    end
    return {
        file = file,
        hash = hash:lower(),
        annotations = doc_settings:readSetting("annotations") or {},
        live = false,
    }
end

-- Collect candidate books: recently-read files with sidecar annotations, plus
-- books we have watermarks for (so deleting a book's last highlight still
-- propagates). Returns a list of
-- {file, hash, annotations (raw sidecar list), live (bool)}.
function BridgeAnnotations.collectBooks(watermarks)
    local books, seen = {}, {}
    local live_file, live_annotations = currentReaderFile()

    local ok_hist, ReadHistory = pcall(require, "readhistory")
    local history = (ok_hist and ReadHistory and ReadHistory.hist) or {}

    local candidates = {}
    if live_file then
        table.insert(candidates, live_file)
    end
    for index, item in ipairs(history) do
        if index > MAX_HISTORY_BOOKS then break end
        if type(item) == "table" and type(item.file) == "string" then
            table.insert(candidates, item.file)
        end
    end

    for _, file in ipairs(candidates) do
        if not seen[file] and lfs.attributes(file, "mode") == "file" then
            seen[file] = true
            local has_sidecar = DocSettings:hasSidecarFile(file)
            local is_live = (file == live_file)
            if has_sidecar or is_live then
                local doc_settings = DocSettings:open(file)
                local hash = bookHash(file, doc_settings)
                if hash then
                    local annotations
                    if is_live and live_annotations then
                        annotations = live_annotations
                    else
                        annotations = doc_settings:readSetting("annotations") or {}
                    end
                    local has_any = type(annotations) == "table" and #annotations > 0
                    local tracked = watermarks[hash] ~= nil
                    if has_any or tracked then
                        table.insert(books, {
                            file = file,
                            hash = hash,
                            annotations = annotations,
                            live = is_live,
                        })
                    end
                end
            end
        end
        if #books >= MAX_BOOKS_PER_EXCHANGE then break end
    end
    return books
end

-- Build one book's exchange payload: full key list + changes since watermark.
-- keys_complete=false marks the key list as non-authoritative for deletion
-- detection (the sweep uses this — a backfill must never delete server data
-- just because a sidecar was momentarily empty/partial).
function BridgeAnnotations.buildBookPayload(book, watermark, keys_complete, ignore_watermark, offset)
    local keys, candidates = {}, {}
    for _, raw in ipairs(book.annotations or {}) do
        local entry = BridgeAnnotations.normalizeEntry(raw)
        if entry then
            table.insert(keys, { k = BridgeAnnotations.buildKey(entry.datetime, entry.pos0), dt = entry.datetime })
            local stamp = entryTimestamp(entry)
            if ignore_watermark or stamp > (watermark or "") then
                table.insert(candidates, entry)
            end
        end
    end

    table.sort(candidates, function(a, b)
        local a_stamp, b_stamp = entryTimestamp(a), entryTimestamp(b)
        if a_stamp ~= b_stamp then return a_stamp < b_stamp end
        if a.datetime ~= b.datetime then return a.datetime < b.datetime end
        return tostring(a.pos0) < tostring(b.pos0)
    end)

    local keys_are_complete = keys_complete ~= false and #keys <= MAX_KEYS_PER_BOOK
    if not keys_are_complete then keys = {} end

    local start_index = math.max(tonumber(offset) or 1, 1)
    local changes = {}
    local max_sent = ""
    local stop_index = math.min(#candidates, start_index + MAX_CHANGES_PER_BOOK - 1)
    for index = start_index, stop_index do
        local entry = candidates[index]
        table.insert(changes, entry)
        local stamp = entryTimestamp(entry)
        if stamp > max_sent then max_sent = stamp end
    end
    return {
        hash = book.hash,
        keys = keys,
        keysComplete = keys_are_complete,
        changes = changes,
    }, max_sent, stop_index < #candidates, stop_index + 1
end

-- ------------------------------------------------------------------
-- Applying server-side changes
-- ------------------------------------------------------------------

local function normalizedText(value)
    if type(value) ~= "string" then return "" end
    return value:gsub("%s+", " "):match("^%s*(.-)%s*$")
end

local function findByIdentity(annotations, entry)
    local found_index, found_count = nil, 0
    if type(entry.datetime) == "string" and entry.datetime ~= "" then
        for index, annotation in ipairs(annotations or {}) do
            if annotation.datetime == entry.datetime then
                found_index, found_count = index, found_count + 1
            end
        end
        if found_count == 1 then return found_index end
    end
    if type(entry.pos0) == "string" and entry.pos0 ~= "" then
        for index, annotation in ipairs(annotations or {}) do
            if BridgeAnnotations.normalizeXPointer(annotation.pos0)
                    == BridgeAnnotations.normalizeXPointer(entry.pos0)
                and (normalizedText(entry.text) == ""
                    or normalizedText(annotation.text) == normalizedText(entry.text))
            then
                return index
            end
        end
    end
    return nil
end

-- Merge toApply into the closed book's sidecar. Returns ack lists.
function BridgeAnnotations.applyToSidecar(book, to_apply)
    local applied, deleted = {}, {}
    local doc_settings = DocSettings:open(book.file)
    local annotations = doc_settings:readSetting("annotations") or {}
    local changed = false

    for _, entry in ipairs(to_apply.add or {}) do
        if type(entry.pos0) == "string" and type(entry.datetime) == "string" then
            local existing = findByIdentity(annotations, entry)
            if existing then
                annotations[existing] = toSidecarEntry(entry)
            else
                table.insert(annotations, toSidecarEntry(entry))
            end
            changed = true
            table.insert(applied, { serverId = entry.serverId, version = entry.version, status = "applied" })
        end
    end

    for _, entry in ipairs(to_apply.edit or {}) do
        if type(entry.datetime) == "string" then
            local existing = findByIdentity(annotations, entry)
            if existing then
                local target = annotations[existing]
                target.drawer = _s(entry.drawer) or target.drawer
                target.color = _s(entry.color) or target.color
                if _s(entry.text) then target.text = entry.text end
                target.note = _s(entry.note)
                if _s(entry.chapter) then target.chapter = entry.chapter end
                if _s(entry.datetimeUpdated) then target.datetime_updated = entry.datetimeUpdated end
            else
                table.insert(annotations, toSidecarEntry(entry))
            end
            changed = true
            table.insert(applied, { serverId = entry.serverId, version = entry.version, status = "applied" })
        end
    end

    for _, entry in ipairs(to_apply.delete or {}) do
        if type(entry.datetime) == "string" then
            local existing = findByIdentity(annotations, entry)
            if existing then
                table.remove(annotations, existing)
                changed = true
            end
            -- Ack even when already absent locally — the tombstone is satisfied.
            table.insert(deleted, { serverId = entry.serverId, status = "applied" })
        end
    end

    if changed then
        doc_settings:saveSetting("annotations", annotations)
        -- The KOHighlights-compatible flag: KOReader re-validates, re-sorts and
        -- re-pages external annotation edits on the next open. makeTrue mirrors
        -- the reference plugins (a plain saveSetting(...,true) also works, but
        -- makeTrue is the documented idiom for boolean flags).
        if type(doc_settings.makeTrue) == "function" then
            doc_settings:makeTrue("annotations_externally_modified")
        else
            doc_settings:saveSetting("annotations_externally_modified", true)
        end
        doc_settings:flush()
    end
    return applied, deleted
end

-- Apply server changes into the CURRENTLY-OPEN book's live annotation list via
-- KOReader's own annotation module, so they render immediately AND persist
-- through KOReader's own close-flush (writing the sidecar directly races that
-- flush and loses — the bug this replaces). The book must be the open one.
function BridgeAnnotations.applyLive(to_apply)
    local ok_rui, ReaderUI = pcall(require, "apps/reader/readerui")
    if not ok_rui or not ReaderUI or not ReaderUI.instance then
        return nil  -- no open book; caller falls back to sidecar/defer
    end
    local ui = ReaderUI.instance
    if not ui.annotation or type(ui.annotation.annotations) ~= "table"
        or not ui.document or not ui.rolling then
        return nil
    end
    local Event = require("ui/event")
    local UIManager = require("ui/uimanager")
    local annotations = ui.annotation.annotations

    local applied, deleted = {}, {}
    local touched = 0

    local function rangeMatches(entry)
        if type(entry.pos0) ~= "string" or type(entry.pos1) ~= "string" then return false end
        local ok0, inside0 = pcall(ui.document.isXPointerInDocument, ui.document, entry.pos0)
        local ok1, inside1 = pcall(ui.document.isXPointerInDocument, ui.document, entry.pos1)
        if not (ok0 and inside0 and ok1 and inside1) then return false end
        if normalizedText(entry.text) == "" or not ui.document.getTextFromXPointers then return true end
        local ok_text, text = pcall(ui.document.getTextFromXPointers, ui.document, entry.pos0, entry.pos1)
        return ok_text and normalizedText(text) == normalizedText(entry.text)
    end

    local function repairRange(entry)
        local text = normalizedText(entry.text)
        if text == "" or not ui.document.findAllText then return false end
        local ok, matches = pcall(ui.document.findAllText, ui.document, text, true, 0, 20, false)
        pcall(ui.document.clearSelection, ui.document)
        if not ok or type(matches) ~= "table" then return false end
        for _, match in ipairs(matches) do
            local candidate = { pos0 = match.start, pos1 = match["end"], text = entry.text }
            if rangeMatches(candidate) then
                entry.pos0, entry.pos1 = candidate.pos0, candidate.pos1
                return true
            end
        end
        return false
    end

    for _, entry in ipairs(to_apply.add or {}) do
        if findByIdentity(annotations, entry) then
            table.insert(applied, { serverId = entry.serverId, version = entry.version, status = "applied" })
        elseif entry.posFormat == "xpointer" and (rangeMatches(entry) or repairRange(entry)) then
            local item = toSidecarEntry(entry)  -- type-guarded (no JSON-null sentinels)
            local ok_add = pcall(function() ui.annotation:addItem(item) end)
            if ok_add then
                pcall(function()
                    ui:handleEvent(Event:new("AnnotationsModified", { item, nb_highlights_added = 1 }))
                end)
                touched = touched + 1
                table.insert(applied, { serverId = entry.serverId, version = entry.version, status = "applied" })
            end
            -- unresolvable / failed add: do NOT ack, so it retries later
        end
    end

    for _, entry in ipairs(to_apply.edit or {}) do
        local idx = findByIdentity(annotations, entry)
        if idx then
            local a = annotations[idx]
            a.drawer = _s(entry.drawer) or a.drawer
            a.color = _s(entry.color) or a.color
            if _s(entry.text) then a.text = entry.text end
            a.note = _s(entry.note)
            if _s(entry.chapter) then a.chapter = entry.chapter end
            if _s(entry.datetimeUpdated) then a.datetime_updated = entry.datetimeUpdated end
            pcall(function()
                ui:handleEvent(Event:new("AnnotationsModified", { a, index_modified = idx }))
            end)
            touched = touched + 1
        end
        -- Ack edits either way so a missing target doesn't loop forever.
        table.insert(applied, { serverId = entry.serverId, version = entry.version, status = "applied" })
    end

    for _, entry in ipairs(to_apply.delete or {}) do
        local idx = findByIdentity(annotations, entry)
        if idx and ui.bookmark and type(ui.bookmark.removeItemByIndex) == "function" then
            pcall(function() ui.bookmark:removeItemByIndex(idx) end)
            touched = touched + 1
        end
        table.insert(deleted, { serverId = entry.serverId, status = "applied" })
    end

    if touched > 0 then
        pcall(function() UIManager:setDirty("all", "ui") end)
    end
    return applied, deleted
end

-- ------------------------------------------------------------------
-- Sync driver
-- ------------------------------------------------------------------

-- Runs one exchange round against the bridge for the given books.
-- `bridge` supplies: api (bridge_api_client), state (LuaSettings),
-- logInfo/logWarn/logErr, and _currentDeviceIdentity().
-- `books` is a list of {file, hash, annotations (sidecar-shaped list), live}.
-- Shared by the periodic sync, the on-close snapshot, and the full sweep.
-- opts.keys_complete=false makes the key lists non-authoritative for deletion
-- (the sweep passes this so a backfill never deletes server data).
function BridgeAnnotations.exchangeBooks(bridge, books, opts)
    opts = opts or {}
    local keys_complete = opts.keys_complete
    if keys_complete == nil then keys_complete = true end
    local ignore_watermark = opts.ignore_watermark
    local device, device_id = bridge:_currentDeviceIdentity()
    local watermarks = bridge.state:readSetting("annotation_watermarks") or {}

    if #books == 0 then
        return { books = 0, uploaded = 0, applied = 0, deleted = 0 }
    end

    local states = {}
    for _, book in ipairs(books) do
        local watermark = watermarks[book.hash] or ""
        if watermark > deviceNow() then
            watermark = ""
            watermarks[book.hash] = ""
        end
        table.insert(states, {
            book = book,
            watermark = watermark,
            offset = 1,
            max_sent = "",
            has_more = false,
        })
    end

    local books_by_hash = {}
    for _, book in ipairs(books) do books_by_hash[book.hash] = book end

    local uploaded_total, applied_total, deleted_total = 0, 0, 0
    local pull_more = {}

    local function applyResponse(response)
        local ack_books = {}
        for _, result in ipairs(response.books or {}) do
            local book = books_by_hash[result.hash]
            local to_apply = result.toApply or {}
            local pending = #(to_apply.add or {}) + #(to_apply.edit or {}) + #(to_apply.delete or {})
            if result.more == true then
                pull_more[result.hash] = true
            else
                pull_more[result.hash] = nil
            end
            if book and pending > 0 then
                local applied, deleted
                if opts.upload_only then
                    bridge:logInfo("Annotation sync: close snapshot, deferring", tostring(pending), "server change(s)")
                elseif book.live then
                    applied, deleted = BridgeAnnotations.applyLive(to_apply)
                    if applied == nil then
                        bridge:logInfo("Annotation sync: open book but no live reader, deferring", tostring(pending))
                    end
                else
                    local ok_apply, a, d = pcall(BridgeAnnotations.applyToSidecar, book, to_apply)
                    if ok_apply then
                        applied, deleted = a, d
                    else
                        bridge:logWarn("Annotation sync: sidecar apply failed:", tostring(a))
                    end
                end
                if applied or deleted then
                    applied_total = applied_total + #(applied or {})
                    deleted_total = deleted_total + #(deleted or {})
                    if #(applied or {}) > 0 or #(deleted or {}) > 0 then
                        table.insert(ack_books, { hash = result.hash, applied = applied or {}, deleted = deleted or {} })
                    end
                end
            end
        end

        if #ack_books > 0 then
            local ok_ack, ack_err = bridge.api:ackAnnotations({
                device = device,
                device_id = device_id,
                books = ack_books,
            })
            if not ok_ack then
                return nil, tostring(ack_err or "Annotation acknowledgment failed")
            end
        end
        return true
    end

    local first_round = true
    while first_round or #states > 0 do
        local payload_books = {}
        local next_states = {}
        for _, state in ipairs(states) do
            local payload, max_sent, has_more, next_offset = BridgeAnnotations.buildBookPayload(
                state.book,
                state.watermark,
                first_round and keys_complete or false,
                ignore_watermark,
                state.offset
            )
            uploaded_total = uploaded_total + #payload.changes
            if max_sent > state.max_sent then state.max_sent = max_sent end
            state.has_more = has_more
            state.offset = next_offset
            table.insert(payload_books, payload)
            if has_more then table.insert(next_states, state) end
        end

        if #payload_books == 0 then break end
        local ok, response = bridge.api:exchangeAnnotations({
            device = device,
            device_id = device_id,
            books = payload_books,
        })
        if not ok then
            return nil, tostring(response or "Annotation exchange failed")
        end
        if response.enabled == false then
            return { books = 0, uploaded = 0, applied = 0, deleted = 0, disabled = true }
        end
        local applied_ok, apply_err = applyResponse(response)
        if not applied_ok then return nil, apply_err end
        states = next_states
        first_round = false
    end

    -- Drain server-side deltas that exceeded one response page. These rounds
    -- never carry an authoritative key list, so they cannot create deletions.
    local pull_round = 0
    while next(pull_more) and pull_round < MAX_PULL_ROUNDS and not opts.upload_only do
        pull_round = pull_round + 1
        local payload_books = {}
        for hash in pairs(pull_more) do
            table.insert(payload_books, { hash = hash, keys = {}, keysComplete = false, changes = {} })
        end
        local ok, response = bridge.api:exchangeAnnotations({
            device = device,
            device_id = device_id,
            books = payload_books,
        })
        if not ok then return nil, tostring(response or "Annotation follow-up failed") end
        local applied_ok, apply_err = applyResponse(response)
        if not applied_ok then return nil, apply_err end
    end

    -- Every outgoing chunk and its ack round succeeded. Advance only now; a
    -- partial failure leaves the old watermark so the idempotent retry can
    -- safely replay all chunks.
    local now = deviceNow()
    for _, state in ipairs(states) do
        -- states is empty after a fully-drained multi-round upload; advancement
        -- is handled from the original book list below.
        state.max_sent = state.max_sent
    end
    for _, book in ipairs(books) do
        local watermark = watermarks[book.hash] or ""
        local _, max_sent = BridgeAnnotations.buildBookPayload(book, watermark, keys_complete, ignore_watermark, 1)
        -- Derive the real maximum only after success; the payload helper's first
        -- chunk is insufficient for large books, so scan normalized entries.
        for _, raw in ipairs(book.annotations or {}) do
            local entry = BridgeAnnotations.normalizeEntry(raw)
            if entry then
                local stamp = entryTimestamp(entry)
                if (ignore_watermark or stamp > watermark) and stamp > max_sent then max_sent = stamp end
            end
        end
        if max_sent > now then max_sent = now end
        if max_sent ~= "" and max_sent > watermark then
            watermarks[book.hash] = max_sent
        elseif watermarks[book.hash] == nil then
            watermarks[book.hash] = ""
        end
    end
    bridge.state:saveSetting("annotation_watermarks", watermarks)
    bridge.state:flush()

    return {
        books = #books,
        uploaded = uploaded_total,
        applied = applied_total,
        deleted = deleted_total,
    }
end

-- Periodic sync entry point: recent books + watermark-tracked books.
function BridgeAnnotations.run(bridge)
    local watermarks = bridge.state:readSetting("annotation_watermarks") or {}
    local books = BridgeAnnotations.collectBooks(watermarks)
    if #books == 0 then
        return { books = 0, uploaded = 0, applied = 0, deleted = 0 }
    end
    return BridgeAnnotations.exchangeBooks(bridge, books)
end

return BridgeAnnotations
