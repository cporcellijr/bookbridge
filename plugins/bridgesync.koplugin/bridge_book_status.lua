-- Reads and writes KOReader's per-book reading status so the bridge can share it
-- between devices.
--
-- Status lives in the book's `.sdr` sidecar under `summary` (`status` plus a
-- `modified` date) and is carried by no other sync channel: KoSync moves position
-- only, and the statistics database has no status column. Writing the sidecar for
-- a book this device has never opened is what makes it show up as in progress in
-- the file browser -- verified on-device, and the minimal `summary`-only sidecar is
-- enough (KOReader reads it live and does not rewrite it).
--
-- Reading POSITION is deliberately untouched here. `percent_finished` is derived
-- from each device's own page geometry and differs between devices for nearly
-- every shared book, so it is never read and never written.

local DocSettings = require("docsettings")
local util = require("util")

local BookStatus = {}

-- KOReader's own vocabulary. Anything else is refused rather than written: these
-- strings go straight into a sidecar, and a bad one is a status the reader has no
-- UI to clear.
BookStatus.VALID = {
    reading = true,
    complete = true,
    abandoned = true,
}

-- The bridge's CLEAR sentinel. It is NOT a KOReader status: applying it removes
-- `summary.status` so the book reads as never opened again. Only the bridge emits
-- it (from Clear Progress); a device never reports it back.
BookStatus.CLEARED = "unread"

function BookStatus.isValid(status)
    return status ~= nil and BookStatus.VALID[tostring(status)] == true
end

-- Statuses this device can APPLY: the real ones plus the clear sentinel.
function BookStatus.isApplicable(status)
    return BookStatus.isValid(status) or tostring(status) == BookStatus.CLEARED
end

function BookStatus.today()
    return os.date("%Y-%m-%d")
end

-- Read one book's status. Never creates a sidecar: a book that has never been
-- opened has no status to report, and materializing one here would invent a
-- reading decision the user never made.
-- @treturn table|nil { status = <string>, modified = <string|nil> }
function BookStatus.read(file)
    if not file or not DocSettings:hasSidecarFile(file) then
        return nil
    end
    local ok, result = pcall(function()
        local doc_settings = DocSettings:open(file)
        local summary = doc_settings:readSetting("summary")
        if type(summary) ~= "table" then
            return nil
        end
        local status = summary.status and tostring(summary.status) or nil
        -- KOReader writes `status = ""` in the wild; it carries no decision.
        if not BookStatus.isValid(status) then
            return nil
        end
        return {
            status = status,
            modified = summary.modified and tostring(summary.modified) or nil,
        }
    end)
    if not ok then
        return nil
    end
    return result
end

-- Write a status into a book's sidecar, creating the sidecar when absent.
-- @treturn boolean, string  written, reason ("unchanged" / "invalid" / error)
function BookStatus.write(file, status, modified)
    if not file then
        return false, "no file"
    end
    if not BookStatus.isApplicable(status) then
        return false, "invalid"
    end
    local clearing = (tostring(status) == BookStatus.CLEARED)

    local ok, written_or_err = pcall(function()
        -- Clearing must not CREATE a sidecar: a book with none already reads as
        -- never opened, and making one just to say "no status" is pure churn.
        if clearing and not DocSettings:hasSidecarFile(file) then
            return false
        end
        local doc_settings = DocSettings:open(file)
        local summary = doc_settings:readSetting("summary")
        if type(summary) ~= "table" then
            summary = {}
        end
        local checksum = util.partialMD5(file)
        if not checksum then error("Could not calculate book checksum", 0) end
        local checksum_changed = doc_settings:readSetting("partial_md5_checksum") ~= checksum
        local status_changed = (clearing and summary.status ~= nil)
            or (not clearing and summary.status ~= status)
        if not status_changed and not checksum_changed then
            return false
        end
        -- Only the status goes; a rating or note the reader wrote in the same
        -- summary block is theirs and survives the clear.
        -- Written long-hand on purpose: `clearing and nil or status` always
        -- yields `status`, because nil is falsy and the `or` branch takes over.
        if status_changed then
            if clearing then
                summary.status = nil
            else
                summary.status = status
            end
            summary.modified = modified or BookStatus.today()
            doc_settings:saveSetting("summary", summary)
        end
        -- A status-only sidecar can make KOReader inherit the previous book's checksum.
        doc_settings:saveSetting("partial_md5_checksum", checksum)
        doc_settings:flush()
        return true
    end)

    if not ok then
        return false, tostring(written_or_err or "write failed")
    end
    if not written_or_err then
        return false, "unchanged"
    end
    return true
end

-- Load a sidecar's metadata table straight off disk.
--
-- Needed for a sidecar whose book file is gone: mirror-mode delivery removes the
-- ebook but leaves the `.sdr` behind, and on a real device most statused sidecars
-- are in that state. DocSettings cannot help there (it is keyed by the book path),
-- but the sidecar itself carries `partial_md5_checksum` -- the same identity the
-- bridge keys on -- so the status is still reportable.
--
-- The file is a KOReader-written `return { ... }` chunk, which is exactly how
-- DocSettings itself loads it.
local function loadSidecarTable(meta_path)
    local chunk = loadfile(meta_path)
    if not chunk then
        return nil
    end
    local ok, data = pcall(chunk)
    if not ok or type(data) ~= "table" then
        return nil
    end
    return data
end

local function statusFromSidecarTable(data)
    local summary = data and data.summary
    if type(summary) ~= "table" then
        return nil
    end
    local status = summary.status and tostring(summary.status) or nil
    if not BookStatus.isValid(status) then
        return nil
    end
    return {
        status = status,
        modified = summary.modified and tostring(summary.modified) or nil,
    }
end

-- Read an orphaned sidecar directory, returning its md5 and status.
-- @treturn table|nil { md5 = , status = , modified = }
function BookStatus.readOrphanSidecar(sdr_dir, lfs_impl)
    local lfs = lfs_impl or require("libs/libkoreader-lfs")
    -- lfs.dir returns (iterator, directory_object) and the iterator REQUIRES that
    -- second value as its state -- capturing only the first yields
    -- "directory metatable expected, got nil". Keeping the call inside the loop
    -- header preserves all of its return values; an unreadable directory is
    -- caught by the surrounding pcall.
    local found
    pcall(function()
        for entry in lfs.dir(sdr_dir) do
            if entry:match("^metadata%..*%.lua$") then
                local data = loadSidecarTable(sdr_dir .. "/" .. entry)
                local md5 = data and data.partial_md5_checksum
                local entry_status = statusFromSidecarTable(data)
                if md5 and entry_status then
                    found = {
                        md5 = tostring(md5),
                        status = entry_status.status,
                        modified = entry_status.modified,
                    }
                    return
                end
            end
        end
    end)
    return found
end

-- Collect this device's statuses for every managed book that has one.
--
-- Two sources, in priority order:
--   1. books whose file is present (keyed by the hash index's content hash),
--   2. sidecars whose book file is gone, keyed by the md5 the sidecar stores.
-- (2) is not an edge case: on a real device it is the large majority, because
-- removing a book from the bridge deletes the ebook and leaves the sidecar.
--
-- @param hash_index table  md5 -> absolute file path
-- @param opts table        { dir = <managed folder>, lfs = <injected lfs> }
-- @treturn table  { { md5 = , status = , modified = }, ... }
function BookStatus.collect(hash_index, opts)
    opts = opts or {}
    local by_md5 = {}

    for hash, path in pairs(hash_index or {}) do
        local entry = BookStatus.read(path)
        if entry then
            by_md5[hash] = {
                md5 = hash,
                status = entry.status,
                modified = entry.modified,
            }
        end
    end

    if opts.dir then
        local lfs = opts.lfs or require("libs/libkoreader-lfs")
        -- See readOrphanSidecar: lfs.dir's second return value is the iterator's
        -- state, so the call stays in the loop header.
        pcall(function()
            for entry in lfs.dir(opts.dir) do
                if entry ~= "." and entry ~= ".." and entry:match("%.sdr$") then
                    local found = BookStatus.readOrphanSidecar(opts.dir .. "/" .. entry, lfs)
                    -- A book whose file is present already reported above, with a
                    -- hash computed from the actual bytes; never let a stale
                    -- sidecar value displace it.
                    if found and not by_md5[found.md5] then
                        by_md5[found.md5] = found
                    end
                end
            end
        end)
    end

    local books = {}
    for _, entry in pairs(by_md5) do
        table.insert(books, entry)
    end
    table.sort(books, function(a, b) return tostring(a.md5) < tostring(b.md5) end)
    return books
end

-- Apply the bridge's resolved statuses to local files.
--
-- `skip_path` is the document currently open in the reader. Its DocSettings are
-- held in memory and rewritten wholesale when the book closes, so a write here
-- would simply be discarded; main.lua re-runs the apply pass after close instead.
--
-- @param hash_index table  md5 -> absolute file path
-- @param entries table     { { md5 = , status = , modified = }, ... }
-- @param opts table        { skip_path = <path|nil> }
-- @treturn table  { applied = , unchanged = , deferred = , skipped = , errors = }
function BookStatus.apply(hash_index, entries, opts)
    opts = opts or {}
    local totals = { applied = 0, unchanged = 0, deferred = 0, skipped = 0, errors = 0 }
    local skip_path = opts.skip_path

    for _, entry in ipairs(entries or {}) do
        local md5 = entry and entry.md5 and tostring(entry.md5) or nil
        local path = md5 and (hash_index or {})[md5] or nil
        if not path then
            totals.skipped = totals.skipped + 1
        elseif skip_path and path == skip_path then
            totals.deferred = totals.deferred + 1
        elseif not BookStatus.isApplicable(entry.status) then
            totals.skipped = totals.skipped + 1
        else
            local written, reason = BookStatus.write(path, entry.status, entry.modified)
            if written then
                totals.applied = totals.applied + 1
            elseif reason == "unchanged" then
                totals.unchanged = totals.unchanged + 1
            else
                totals.errors = totals.errors + 1
            end
        end
    end

    return totals
end

return BookStatus
