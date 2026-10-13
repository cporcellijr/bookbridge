-- Executable coverage for bridge_book_status.lua, the sidecar reading-status
-- reader/writer. DocSettings is faked with an in-memory sidecar store so the
-- read/write/skip rules are exercised without a device.

local plugin_dir = assert(arg[1], "plugin directory argument required")
package.path = plugin_dir .. "/?.lua;" .. package.path

-- Fake sidecar store: path -> settings table. A path absent from `sidecars` has
-- no sidecar on disk, which is the "delivered but never opened" case.
local sidecars = {}
local flushed = {}
local checksum = "506e3e2bc7cd723fcb14afef476585de"
local hash_failure = false
package.preload["util"] = function()
    return { partialMD5 = function() return not hash_failure and checksum or nil end }
end

local function resetStore()
    sidecars = {}
    flushed = {}
end

local fake_docsettings = {
    hasSidecarFile = function(_, file)
        return sidecars[file] ~= nil
    end,
    open = function(_, file)
        local store = sidecars[file]
        local pending = {}
        if store then
            for k, v in pairs(store) do pending[k] = v end
        end
        return {
            readSetting = function(_, key) return pending[key] end,
            saveSetting = function(_, key, value) pending[key] = value end,
            flush = function()
                sidecars[file] = pending
                flushed[file] = (flushed[file] or 0) + 1
            end,
        }
    end,
}
package.preload["docsettings"] = function() return fake_docsettings end

local BookStatus = require("bridge_book_status")

-- Vocabulary is closed: values go straight into a sidecar.
do
    assert(BookStatus.isValid("reading"))
    assert(BookStatus.isValid("complete"))
    assert(BookStatus.isValid("abandoned"))
    assert(not BookStatus.isValid(""), "empty status carries no decision")
    assert(not BookStatus.isValid("finished"), "non-KOReader spelling must be refused")
    assert(not BookStatus.isValid(nil))
end

-- Reading a book with no sidecar must not create one.
do
    resetStore()
    assert(BookStatus.read("/books/never-opened.epub") == nil)
    assert(sidecars["/books/never-opened.epub"] == nil,
        "reading must never materialize a sidecar")
end

-- Reading picks up status and modified date.
do
    resetStore()
    sidecars["/books/a.epub"] = { summary = { status = "complete", modified = "2026-05-05" } }
    local entry = BookStatus.read("/books/a.epub")
    assert(entry and entry.status == "complete" and entry.modified == "2026-05-05")
end

-- KOReader writes status = "" in the wild; it must read as no status at all.
do
    resetStore()
    sidecars["/books/empty.epub"] = { summary = { status = "", modified = "2026-09-18" } }
    assert(BookStatus.read("/books/empty.epub") == nil)
end

-- A sidecar with no summary block at all.
do
    resetStore()
    sidecars["/books/nosummary.epub"] = { percent_finished = 0.4 }
    assert(BookStatus.read("/books/nosummary.epub") == nil)
end

-- Writing to a never-opened book creates the sidecar. This is the whole feature.
do
    resetStore()
    local written = BookStatus.write("/books/fresh.epub", "reading", "2026-09-18")
    assert(written, "write must succeed for a book with no sidecar")
    local summary = sidecars["/books/fresh.epub"].summary
    assert(summary.status == "reading" and summary.modified == "2026-09-18")
    -- KOReader 2026.07 can inherit the preceding book's cached checksum when
    -- a plugin-created sidecar makes this book look previously opened.
    local reader = setmetatable({ doc_settings = sidecars["/books/fresh.epub"] }, {
        __index = { md5_checksum = "776802041599e3d31d5eaf580ff3ca30" },
    })
    local used_checksum = reader.doc_settings.partial_md5_checksum or reader.md5_checksum
    assert(used_checksum == checksum, "Goddess inherited Merfolk's checksum on first open")
end

-- Repair an already-crossed identity even when status agrees, retaining reading data.
do
    resetStore()
    sidecars["/books/goddess.epub"] = {
        partial_md5_checksum = "776802041599e3d31d5eaf580ff3ca30",
        summary = { status = "reading", modified = "2026-10-05", rating = 5 },
        annotations = { "highlight" }, last_xpointer = "/body/DocFragment[46]/body/div/p[66]",
        percent_finished = 0.7939,
    }
    assert(BookStatus.write("/books/goddess.epub", "reading", "2026-10-13"))
    local store = sidecars["/books/goddess.epub"]
    assert(store.partial_md5_checksum == checksum)
    assert(store.percent_finished == 0.7939 and store.annotations[1] == "highlight")
    assert(store.last_xpointer == "/body/DocFragment[46]/body/div/p[66]")
    assert(store.summary.modified == "2026-10-05" and store.summary.rating == 5)
    local written, reason = BookStatus.write("/books/goddess.epub", "reading")
    assert(not written and reason == "unchanged" and flushed["/books/goddess.epub"] == 1)
end

-- Failure to hash must not publish a status-only metadata file.
do
    resetStore()
    hash_failure = true
    local written, reason = BookStatus.write("/books/unreadable.epub", "reading")
    hash_failure = false
    assert(not written and reason ~= "unchanged")
    assert(sidecars["/books/unreadable.epub"] == nil)
end

-- Writing preserves the rest of an existing sidecar.
do
    resetStore()
    sidecars["/books/keep.epub"] = {
        summary = { status = "reading", modified = "2026-01-01" },
        annotations = { "keep me" },
        percent_finished = 0.5,
    }
    assert(BookStatus.write("/books/keep.epub", "complete", "2026-09-18"))
    local store = sidecars["/books/keep.epub"]
    assert(store.summary.status == "complete")
    assert(store.annotations[1] == "keep me", "unrelated sidecar keys must survive")
    assert(store.percent_finished == 0.5, "position data must not be touched")
end

-- A no-op write must not touch the file: a third-party shelf plugin keys its
-- status cache on sidecar mtime, so a pointless rewrite invalidates it for free.
do
    resetStore()
    sidecars["/books/same.epub"] = { partial_md5_checksum = checksum,
        summary = { status = "reading", modified = "2026-01-01" } }
    local written, reason = BookStatus.write("/books/same.epub", "reading")
    assert(not written and reason == "unchanged")
    assert((flushed["/books/same.epub"] or 0) == 0, "unchanged status must not flush")
end

-- An invalid status is refused rather than written.
do
    resetStore()
    local written, reason = BookStatus.write("/books/bad.epub", "finished-ish")
    assert(not written and reason == "invalid")
    assert(sidecars["/books/bad.epub"] == nil)
end

-- collect() reports only books that actually carry a status.
do
    resetStore()
    sidecars["/books/one.epub"] = { summary = { status = "reading", modified = "2026-02-02" } }
    sidecars["/books/two.epub"] = { summary = { status = "complete", modified = "2026-03-03" } }
    sidecars["/books/three.epub"] = { summary = { status = "" } }
    local index = {
        aaa = "/books/one.epub",
        bbb = "/books/two.epub",
        ccc = "/books/three.epub",
        ddd = "/books/never-opened.epub",
    }
    local books = BookStatus.collect(index)
    assert(#books == 2, "only statused books are reported, got " .. #books)
    assert(books[1].md5 == "aaa" and books[1].status == "reading")
    assert(books[2].md5 == "bbb" and books[2].modified == "2026-03-03")
end

-- ---------------------------------------------------------------------------
-- Orphaned sidecars: book file gone, .sdr left behind by mirror deletion.
-- On a real device these are the MAJORITY of statused sidecars, and the status
-- still matters because the book may be present on another device.
-- ---------------------------------------------------------------------------

-- A fake managed folder: dir name -> entries, plus files written by the test.
local disk = {}

-- Mirrors REAL LuaFileSystem: lfs.dir returns (iterator, directory_object) and
-- the iterator requires that second value as its state. A mock that returned a
-- single closure let a bug through to a device -- capturing only the first return
-- value raised "directory metatable expected, got nil" on the real thing. This
-- mock fails the same way, so the bug cannot come back silently.
local fake_lfs = {
    dir = function(path)
        local entries = disk[path]
        if not entries then error("no such dir: " .. tostring(path)) end
        local state = { i = 0, entries = entries }
        local function iter(s)
            if s == nil then
                error("directory metatable expected, got nil", 2)
            end
            s.i = s.i + 1
            return s.entries[s.i]
        end
        return iter, state
    end,
}

-- loadfile() is what reads an orphaned sidecar, so the test writes real files.
local tmp_root = os.getenv("TMPDIR") or os.getenv("TEMP") or "/tmp"
tmp_root = tmp_root:gsub("\\", "/") .. "/bridgesync-status-test"
os.execute('mkdir "' .. tmp_root:gsub("/", package.config:sub(1, 1)) .. '" 2>' ..
    (package.config:sub(1, 1) == "\\" and "nul" or "/dev/null"))

local function writeSidecar(name, body)
    local dir = tmp_root .. "/" .. name
    os.execute('mkdir "' .. dir:gsub("/", package.config:sub(1, 1)) .. '" 2>' ..
        (package.config:sub(1, 1) == "\\" and "nul" or "/dev/null"))
    local path = dir .. "/metadata.epub.lua"
    local fh = assert(io.open(path, "wb"))
    fh:write(body)
    fh:close()
    disk[dir] = { ".", "..", "metadata.epub.lua" }
    return dir
end

-- An orphaned sidecar reports the md5 it stores.
do
    local dir = writeSidecar("Orphan.sdr", [[
return {
    ["partial_md5_checksum"] = "6b9b798b885a8c9858c230d8d63ac49b",
    ["percent_finished"] = 0.025,
    ["summary"] = {
        ["modified"] = "2026-09-06",
        ["status"] = "complete",
    },
}
]])
    local found = BookStatus.readOrphanSidecar(dir, fake_lfs)
    assert(found, "orphaned sidecar must be readable")
    assert(found.md5 == "6b9b798b885a8c9858c230d8d63ac49b", found.md5)
    assert(found.status == "complete" and found.modified == "2026-09-06")
end

-- No md5 stored -> nothing reportable (the bridge keys on md5).
do
    local dir = writeSidecar("NoMd5.sdr", [[
return {
    ["summary"] = { ["status"] = "reading", ["modified"] = "2026-01-01" },
}
]])
    assert(BookStatus.readOrphanSidecar(dir, fake_lfs) == nil)
end

-- No status -> nothing to share.
do
    local dir = writeSidecar("NoStatus.sdr", [[
return {
    ["partial_md5_checksum"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ["percent_finished"] = 0.5,
}
]])
    assert(BookStatus.readOrphanSidecar(dir, fake_lfs) == nil)
end

-- A corrupt sidecar must not take the sync down.
do
    local dir = writeSidecar("Corrupt.sdr", "this is not lua {{{")
    assert(BookStatus.readOrphanSidecar(dir, fake_lfs) == nil)
end

-- collect() merges both sources, and a present book's content hash wins.
do
    resetStore()
    sidecars["/books/present.epub"] = {
        summary = { status = "reading", modified = "2026-02-02" },
    }
    local orphan = writeSidecar("Gone.sdr", [[
return {
    ["partial_md5_checksum"] = "cccccccccccccccccccccccccccccccc",
    ["summary"] = { ["modified"] = "2026-09-06", ["status"] = "complete" },
}
]])
    -- The present book's own sidecar claims a DIFFERENT md5 than the content
    -- hash; the content hash must not be displaced by it.
    local present = writeSidecar("Present.sdr", [[
return {
    ["partial_md5_checksum"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ["summary"] = { ["modified"] = "2026-02-02", ["status"] = "reading" },
}
]])
    disk[tmp_root] = { ".", "..", "Gone.sdr", "Present.sdr", "notes.txt" }

    local books = BookStatus.collect(
        { ["aaaa1111aaaa1111aaaa1111aaaa1111"] = "/books/present.epub" },
        { dir = tmp_root, lfs = fake_lfs }
    )
    local by = {}
    for _, b in ipairs(books) do by[b.md5] = b end

    assert(by["aaaa1111aaaa1111aaaa1111aaaa1111"], "present book must report by content hash")
    assert(by["aaaa1111aaaa1111aaaa1111aaaa1111"].status == "reading")
    assert(by["cccccccccccccccccccccccccccccccc"], "orphaned sidecar must be reported")
    assert(by["cccccccccccccccccccccccccccccccc"].status == "complete")
    assert(orphan and present)
end

-- A missing managed folder must not raise.
do
    resetStore()
    local books = BookStatus.collect({}, { dir = "/definitely/not/here", lfs = fake_lfs })
    assert(type(books) == "table" and #books == 0)
end

-- apply() writes winners, skips unknown md5s, and defers the open document.
do
    resetStore()
    sidecars["/books/open.epub"] = { summary = { status = "reading", modified = "2026-01-01" } }
    sidecars["/books/agree.epub"] = { partial_md5_checksum = checksum,
        summary = { status = "complete", modified = "2026-01-01" } }
    local index = {
        aaa = "/books/fresh.epub",     -- no sidecar yet: gets one
        bbb = "/books/open.epub",      -- currently open: deferred
        ccc = "/books/agree.epub",     -- already agrees: unchanged
    }
    local totals = BookStatus.apply(index, {
        { md5 = "aaa", status = "reading", modified = "2026-09-18" },
        { md5 = "bbb", status = "complete", modified = "2026-09-18" },
        { md5 = "ccc", status = "complete", modified = "2026-09-18" },
        { md5 = "zzz", status = "reading", modified = "2026-09-18" },  -- not on this device
        { md5 = "aaa", status = "nonsense", modified = "2026-09-18" }, -- refused
    }, { skip_path = "/books/open.epub" })

    assert(totals.applied == 1, "applied=" .. totals.applied)
    assert(totals.deferred == 1, "deferred=" .. totals.deferred)
    assert(totals.unchanged == 1, "unchanged=" .. totals.unchanged)
    assert(totals.skipped == 2, "skipped=" .. totals.skipped)
    assert(totals.errors == 0, "errors=" .. totals.errors)

    assert(sidecars["/books/fresh.epub"].summary.status == "reading")
    assert(sidecars["/books/open.epub"].summary.status == "reading",
        "the open document's sidecar must be left alone")
end

-- apply() tolerates an empty or missing entry list.
do
    resetStore()
    local totals = BookStatus.apply({}, nil, {})
    assert(totals.applied == 0 and totals.errors == 0)
end

-- ---------------------------------------------------------------------------
-- The clear sentinel ("unread"): removes a status without inventing a sidecar.
-- ---------------------------------------------------------------------------

do
    assert(not BookStatus.isValid("unread"), "'unread' is NOT a KOReader status")
    assert(BookStatus.isApplicable("unread"), "but a device may apply it")
    assert(BookStatus.isApplicable("complete"))
    assert(not BookStatus.isApplicable("nonsense"))
end

-- Clearing removes the status and leaves the rest of the sidecar alone.
do
    resetStore()
    sidecars["/books/clear.epub"] = {
        summary = { status = "complete", modified = "2026-01-01", rating = 5, note = "loved it" },
        annotations = { "keep" },
        percent_finished = 1,
    }
    assert(BookStatus.write("/books/clear.epub", "unread", "2026-09-18"))
    local store = sidecars["/books/clear.epub"]
    assert(store.summary.status == nil, "status must be gone")
    assert(store.summary.rating == 5, "a rating the reader wrote must survive")
    assert(store.summary.note == "loved it", "a note the reader wrote must survive")
    assert(store.annotations[1] == "keep")
    assert(store.percent_finished == 1, "position data must not be touched")
end

-- Clearing a book with no sidecar must NOT create one: it already reads as new.
do
    resetStore()
    local written, reason = BookStatus.write("/books/untouched.epub", "unread")
    assert(not written and reason == "unchanged", tostring(reason))
    assert(sidecars["/books/untouched.epub"] == nil, "clearing must not create a sidecar")
end

-- Clearing a sidecar that has no status is a no-op, not a rewrite.
do
    resetStore()
    sidecars["/books/nostatus.epub"] = { percent_finished = 0.3, partial_md5_checksum = checksum }
    local written, reason = BookStatus.write("/books/nostatus.epub", "unread")
    assert(not written and reason == "unchanged")
    assert((flushed["/books/nostatus.epub"] or 0) == 0)
end

-- apply() routes the sentinel through like any other status.
do
    resetStore()
    sidecars["/books/a.epub"] = { summary = { status = "complete", modified = "2026-01-01" } }
    local totals = BookStatus.apply(
        { aaa = "/books/a.epub" },
        { { md5 = "aaa", status = "unread", modified = "2026-09-18" } },
        {}
    )
    assert(totals.applied == 1, "applied=" .. totals.applied)
    assert(sidecars["/books/a.epub"].summary.status == nil)
end

-- A device never REPORTS the sentinel: collect() only reads real statuses.
do
    resetStore()
    sidecars["/books/b.epub"] = { summary = { status = "unread", modified = "2026-09-18" } }
    local books = BookStatus.collect({ bbb = "/books/b.epub" })
    assert(#books == 0, "'unread' in a sidecar is not a status to report")
end

print("BridgeSync book-status Lua tests passed")
