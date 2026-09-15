local plugin_dir = assert(arg[1], "plugin directory argument required")
local settings_dir = assert(arg[2], "settings directory argument required")
package.path = plugin_dir .. "/?.lua;" .. package.path

local function preload(name, factory)
    package.preload[name] = factory
end

local function empty_module()
    return {}
end

preload("ui/widget/confirmbox", empty_module)
preload("ui/widget/infomessage", empty_module)
preload("ui/widget/inputdialog", empty_module)
preload("ui/network/manager", function()
    return {
        isConnected = function() return true end,
    }
end)
preload("ui/trapper", empty_module)
preload("bit", empty_module)

local dns_lookups = {}
preload("socket", function()
    return {
        dns = {
            toip = function(host)
                dns_lookups[#dns_lookups + 1] = host
                return nil
            end,
        },
    }
end)

preload("gettext", function()
    return function(value) return value end
end)

preload("datastorage", function()
    return {
        getSettingsDir = function() return settings_dir end,
    }
end)

preload("dispatcher", function()
    return {
        registerAction = function() end,
    }
end)

local init_scheduled = {}
local init_unscheduled = {}
preload("ui/uimanager", function()
    return {
        scheduleIn = function(_, _, callback) init_scheduled[#init_scheduled + 1] = callback end,
        unschedule = function(_, callback) init_unscheduled[#init_unscheduled + 1] = callback end,
    }
end)

preload("ui/widget/container/widgetcontainer", function()
    local WidgetContainer = {}
    function WidgetContainer:extend(definition)
        definition.__index = definition
        function definition:new(instance)
            return setmetatable(instance or {}, self)
        end
        return definition
    end
    return WidgetContainer
end)

preload("logger", function()
    return {
        info = function() end,
        warn = function() end,
        err = function() end,
    }
end)

preload("libs/libkoreader-lfs", function()
    return {
        attributes = function(path, attribute)
            if path == "/mnt/onboard" and attribute == "mode" then
                return "directory"
            end
            if attribute == "size" then
                local handle = io.open(path, "rb")
                if handle then
                    local size = handle:seek("end")
                    handle:close()
                    return size
                end
            end
            -- Real lfs.attributes reports failure as nil + message + errno. A
            -- bare nil here hid the 0.6.3 fresh-install crash: with no log file
            -- yet, tonumber(lfs.attributes(path, "size")) expanded those extra
            -- returns into tonumber's base argument and killed init().
            return nil, path .. ": No such file or directory", 2
        end,
    }
end)

preload("ffi/sha2", function()
    return {
        md5 = function(value) return value end,
        sha256 = function() return string.rep("a", 64) end,
    }
end)

preload("ffi/util", function()
    return {
        template = function(value, ...)
            local args = { ... }
            return tostring(value):gsub("%%(%d+)", function(index)
                return tostring(args[tonumber(index)] or "")
            end)
        end,
    }
end)

preload("string.buffer", function()
    return {
        encode = function(value) return value end,
        decode = function(value) return value end,
    }
end)

preload("json", function()
    return {
        encode = function() return "{}" end,
        decode = function(value)
            if value == "__partial_session_response__" then
                return {
                    accepted = 1,
                    rejected = 1,
                    results = {
                        { index = 1, session_id = "session-accepted", accepted = true },
                        { index = 2, session_id = "session-rejected", accepted = false,
                            reason = "book_not_found" },
                    },
                }
            end
            return {}
        end,
    }
end)

preload("luasettings", function()
    local Settings = {}
    Settings.__index = Settings
    function Settings:readSetting()
        return nil
    end
    function Settings:saveSetting(key, value)
        self.data[key] = value
    end
    function Settings:flush() end

    return {
        open = function()
            return setmetatable({ data = {} }, Settings)
        end,
    }
end)

local uploaded_log_payloads = {}

preload("bridge_api_client", function()
    local APIClient = {}
    function APIClient:new()
        return setmetatable({}, { __index = self })
    end
    function APIClient:init(server_url, username, key, log_callback)
        self.server_url = server_url
        self.username = username
        self.key = key
        self.log_callback = log_callback
    end
    function APIClient:uploadSessions()
        return true, 200, ""
    end
    function APIClient:uploadClientLogs(payload)
        uploaded_log_payloads[#uploaded_log_payloads + 1] = payload
        return true, 200, ""
    end
    return APIClient
end)

local sqlite_values = {
    server_url = "http://bridge:5758",
    username = "reader",
    key = "secret",
}

preload("bridge_sqlite_state", function()
    local BridgeSqliteState = {}
    function BridgeSqliteState:new()
        return setmetatable({}, { __index = self })
    end
    function BridgeSqliteState:is_available()
        return true
    end
    function BridgeSqliteState:init()
        return true
    end
    function BridgeSqliteState:get_setting(key, default)
        if key == "migration_done" then
            return true
        end
        if sqlite_values[key] ~= nil then
            return sqlite_values[key]
        end
        return default
    end
    function BridgeSqliteState:set_setting(key, value)
        sqlite_values[key] = value
        return true
    end
    function BridgeSqliteState:prune_uploaded_sessions()
        return true
    end
    function BridgeSqliteState:get_pending_sessions()
        return {}
    end
    return BridgeSqliteState
end)

preload("bridge_sync_coordinator", function()
    local Coordinator = {}
    function Coordinator:new()
        return setmetatable({}, { __index = self })
    end
    return Coordinator
end)

local annotation_exchange_ok = true
preload("bridge_annotations", function()
    return {
        resolveBookHash = function() return string.rep("a", 32) end,
        collectBookByFile = function() return nil end,
        exchangeBooks = function()
            if annotation_exchange_ok then
                return { uploaded = 1, applied = 0, deleted = 0 }
            end
            return nil, "offline"
        end,
    }
end)
preload("bridge_sweep", empty_module)
preload("bridge_stats_batches", empty_module)
preload("bridge_version", empty_module)
preload("bridge_sessions", empty_module)

local BridgeSync = require("main")
local extracted_paths = {}
local fake_archiver = {
    Reader = {
        new = function()
            local entries = {
                { path = "bridgesync.koplugin/_meta.lua" },
                { path = "bridgesync.koplugin/main.lua" },
            }
            return {
                open = function() return true end,
                close = function() end,
                iterate = function()
                    local index = 0
                    return function()
                        index = index + 1
                        return entries[index]
                    end
                end,
                extractToPath = function(_, _, target)
                    extracted_paths[#extracted_paths + 1] = target
                    return true
                end,
            }
        end,
    },
}
assert(BridgeSync._extractWithArchiver(fake_archiver, "update.zip", "/stage"))
assert(extracted_paths[1] == "/stage/bridgesync.koplugin/_meta.lua",
    "current KOReader archiver must extract entries into staging")

-- A failed entry must fail the whole extraction. Returning success on a partial
-- tree let _installPluginZip stage a plugin missing modules other than
-- _meta.lua/main.lua and rename it over the backup, bricking the plugin.
local partial_archiver = {
    Reader = {
        new = function()
            local entries = {
                { path = "bridgesync.koplugin/_meta.lua" },
                { path = "bridgesync.koplugin/main.lua" },
                { path = "bridgesync.koplugin/bridge_annotations.lua" },
            }
            return {
                err = nil,
                open = function() return true end,
                close = function() end,
                iterate = function()
                    local index = 0
                    return function()
                        index = index + 1
                        return entries[index]
                    end
                end,
                -- Third entry fails and, as some archiver builds do, sets no err.
                extractToPath = function(_, path)
                    return path ~= "bridgesync.koplugin/bridge_annotations.lua"
                end,
            }
        end,
    },
}
local partial_ok, partial_err = BridgeSync._extractWithArchiver(
    partial_archiver, "update.zip", "/stage")
assert(not partial_ok, "a failed entry must not report extraction success")
assert(type(partial_err) == "string" and partial_err:find("bridge_annotations", 1, true),
    "the failure must name the entry that could not be extracted")

local archive_path = settings_dir .. "/bridgesync-update-test.zip"
local archive = assert(io.open(archive_path, "wb"))
archive:write("plugin archive")
archive:close()
assert(BridgeSync._verifyPluginArchive(archive_path, string.rep("a", 64)),
    "matching update checksums must pass")
local digest_ok, digest_err = BridgeSync._verifyPluginArchive(archive_path, string.rep("b", 64))
assert(not digest_ok and digest_err:find("checksum mismatch", 1, true),
    "mismatched update checksums must fail closed")
os.remove(archive_path)

local metadata_path = settings_dir .. "/bridgesync-update-meta.lua"
local metadata = assert(io.open(metadata_path, "wb"))
metadata:write('return { name = "bridgesync", version = "0.6.3" }')
metadata:close()
assert(BridgeSync._validatePluginMetadata(metadata_path, "0.6.3"),
    "matching staged BridgeSync metadata must pass")
local metadata_ok, metadata_err = BridgeSync._validatePluginMetadata(metadata_path, "9.9.9")
assert(not metadata_ok and metadata_err:find("does not match", 1, true),
    "a staged plugin with the wrong version must fail closed")
metadata = assert(io.open(metadata_path, "wb"))
metadata:write('return { fullname = "Bridge Sync", name = "other", version = "0.6.3" }')
metadata:close()
metadata_ok, metadata_err = BridgeSync._validatePluginMetadata(metadata_path, "0.6.3")
assert(not metadata_ok and metadata_err:find("not BridgeSync", 1, true),
    "a staged package with the wrong identity must fail closed")
os.remove(metadata_path)

local bridge = BridgeSync:new({
    path = plugin_dir,
    ui = {
        menu = {
            registerToMainMenu = function() end,
        },
    },
})

local ok, init_error = pcall(bridge.init, bridge)
assert(ok, "BridgeSync init failed: " .. tostring(init_error))
assert(bridge.log_path == settings_dir .. "/bridge_sync.log",
    "BridgeSync must initialize log_path before startup logging")

-- A fresh install has no stored annotation_sync_enabled. The settings-version
-- stamp used to sit inside the else-branch of that check, so it was written
-- only for installs that already had the key - never on a first run, leaving
-- every fresh device at settings_version 0 for the first schema migration.
assert(type(sqlite_values.settings_version) == "number"
        and sqlite_values.settings_version >= 1,
    "a fresh install must persist the settings schema version during init")

bridge.server_url = "http://192.168.88.200:5758"
local network_ok, network_err = bridge:_preflightNetwork()
assert(network_ok, tostring(network_err))
assert(#dns_lookups == 0,
    "a literal IPv4 server must bypass DNS resolution")

bridge.server_url = "http://bridge.example:5758"
network_ok, network_err = bridge:_preflightNetwork()
assert(not network_ok and network_err == "DNS lookup failed for bridge.example",
    "hostnames must retain the DNS preflight")
assert(#dns_lookups == 1 and dns_lookups[1] == "bridge.example",
    "the DNS preflight must receive only the hostname")

local handle = assert(io.open(bridge.log_path, "r"),
    "BridgeSync startup did not create bridge_sync.log")
local log_contents = handle:read("*a")
handle:close()
assert(log_contents:find("SQLite state manager initialized", 1, true),
    "BridgeSync startup did not persist its first SQLite log message")

bridge.pending_annotation_closes = {}
bridge:_enqueuePendingAnnotationClose("/books/one.epub", {
    hash = string.rep("1", 32),
    annotations = { { datetime = "now" } },
})
bridge:_enqueuePendingAnnotationClose("/books/one.epub", {
    hash = string.rep("1", 32),
    annotations = { { datetime = "later" } },
})
assert(#bridge.pending_annotation_closes == 1,
    "close annotation snapshots must be deduplicated by book")
assert(sqlite_values.pending_annotation_closes ~= nil,
    "close annotation snapshots must be persisted")

annotation_exchange_ok = false
assert(bridge:_uploadPendingAnnotationCloses() == false)
assert(#bridge.pending_annotation_closes == 1,
    "failed close annotation snapshots must remain queued")
annotation_exchange_ok = true
assert(bridge:_uploadPendingAnnotationCloses() == true)
assert(#bridge.pending_annotation_closes == 0,
    "successful close annotation snapshots must leave the queue")

bridge.close_book_sync_scheduled = true
bridge:_scheduleTask("book_sync", 5, function() end)
bridge:_cancelAutomaticTasks()
assert(#init_unscheduled > 0 and bridge.scheduled_tasks.book_sync == nil,
    "suspend cancellation must unschedule named automatic work")
assert(bridge.needs_wake_sync == true,
    "cancelled book sync must remain eligible after resume")

local oversized_log = assert(io.open(bridge.log_path, "wb"))
oversized_log:write(string.rep("x", 512 * 1024))
oversized_log:close()
bridge:_appendLog("info", "rotated")
local rotated = io.open(bridge.log_path .. ".1", "rb")
assert(rotated, "oversized device logs must rotate to a bounded backup")
rotated:close()
local active_log = assert(io.open(bridge.log_path, "rb"))
assert((active_log:seek("end") or 0) < 1024,
    "active device log must restart small after rotation")
active_log:close()

bridge:logWarn("Book sync completed with one deferred download")
assert(bridge:_uploadDeviceLogTail("book_sync", "partial") == true)
assert(#uploaded_log_payloads == 1)
assert(uploaded_log_payloads[1].operation == "book_sync")
assert(uploaded_log_payloads[1].status == "partial")

bridge.pending_sessions = {
    { session_id = "session-1", abs_id = "book-1" },
}
bridge.sqlite_state.mark_sessions_uploaded = function()
    return false
end
local upload_ok = bridge:_uploadSessions()
assert(upload_ok == false,
    "session upload must fail locally when SQLite acknowledgement cannot be persisted")
assert(#bridge.pending_sessions == 1,
    "unacknowledged sessions must remain queued for retry")

bridge.sqlite_state.mark_sessions_uploaded = function()
    return true
end
upload_ok = bridge:_uploadSessions()
assert(upload_ok == true,
    "session upload must complete once SQLite acknowledgement succeeds")
assert(#bridge.pending_sessions == 0,
    "acknowledged sessions must be removed from the in-memory queue")
assert(#uploaded_log_payloads == 3,
    "each attempted session upload must report its device log tail")
assert(uploaded_log_payloads[2].operation == "session_upload")
assert(uploaded_log_payloads[2].status == "failure")
assert(uploaded_log_payloads[3].status == "success")
local plugin_meta = assert(loadfile(plugin_dir .. "/_meta.lua"))()
assert(uploaded_log_payloads[3].plugin_version == plugin_meta.version,
    "telemetry must report the installed _meta.lua version")
assert(type(sqlite_values.device_log_upload_offset) == "number",
    "successful telemetry must persist the acknowledged log byte offset")

local saw_ack_failure = false
for _, line in ipairs(uploaded_log_payloads[2].lines or {}) do
    if line:find("local SQLite acknowledgement failed", 1, true) then
        saw_ack_failure = true
        break
    end
end
assert(saw_ack_failure,
    "failure telemetry must include the local SQLite acknowledgement diagnostic")

bridge._loadStateItems = function()
    return {
        ["kobo-book"] = {
            local_path = "/mnt/onboard/Koreaderbooks/Title.epub",
            filename = "Title.epub",
            content_hash = "hash-1",
        },
    }
end
assert(bridge:_resolveAbsId("/mnt/onboard/KoreaderBooks/Title.epub") == "kobo-book",
    "managed Kobo paths must resolve across case-only directory differences")
bridge.ui.document = { file = "/mnt/onboard/KoreaderBooks/Title.epub" }
assert(bridge:_isCurrentDocument("/mnt/onboard/Koreaderbooks/Title.epub"),
    "the open-book guard must compare managed Kobo paths case-insensitively")

local saved_items = nil
bridge.delete_removed_books = true
bridge._ensureDirectory = function() return true end
bridge._getStateScalar = function() return "old-revision" end
bridge.api.getManifest = function()
    return true, { revision = "new-revision", books = {} }
end
bridge._saveState = function(_, items)
    saved_items = items
    return true
end
bridge._updateCollections = function() end
bridge._isCurrentDocument = function() return false end
bridge._deleteManagedFile = function() return false, "permission denied" end
local delete_result = bridge:_runSync()
assert(delete_result.deleted == 0 and delete_result.errors == 1,
    "failed managed-file removal must be reported as an error, not a deletion")
assert(saved_items["kobo-book"] ~= nil,
    "failed managed-file removal must retain state for a later retry")

local marked_session_ids = nil
bridge.pending_sessions = {
    { session_id = "session-accepted", abs_id = "book-1" },
    { session_id = "session-rejected", abs_id = "missing-book" },
}
bridge.api.uploadSessions = function()
    return true, 200, "__partial_session_response__"
end
bridge.sqlite_state.mark_sessions_uploaded = function(_, session_ids)
    marked_session_ids = session_ids
    return true
end
upload_ok = bridge:_uploadSessions()
assert(upload_ok == false,
    "a partial server acknowledgement must keep the upload job retryable")
assert(#marked_session_ids == 1 and marked_session_ids[1] == "session-accepted",
    "only server-accepted sessions may be acknowledged in local SQLite")
assert(#bridge.pending_sessions == 1
        and bridge.pending_sessions[1].session_id == "session-rejected",
    "server-rejected sessions must remain queued for recovery")

-- A forked child must never fork again. BridgeSync wires bridge_api_client's
-- request_runner to _runInSubprocess, so every non-download HTTP request issued
-- from inside an already-forked child re-entered this function. The child
-- inherits the running coroutine, so it forked a grandchild and then yielded
-- itself back into a copy of the parent's UIManager loop, leaving a second
-- instance of the app on the same screen and input devices: an outright crash
-- on Kindle (#401), and on Android a child that died before writing a result,
-- which surfaced as a bare "Authentication failed" (#370).
local FFIUtilStub = require("ffi/util")
local fork_calls = 0
local child_output = nil

local function stub_fork(run_child)
    FFIUtilStub.runInSubProcess = function(child_func)
        fork_calls = fork_calls + 1
        child_output = nil
        -- The child body runs in-process, which is what makes a nested fork
        -- observable: the coroutine is still running, exactly as it is inside
        -- a real forked child.
        if run_child then child_func(4242, 7) end
        return 4242, 8
    end
end
FFIUtilStub.writeToFD = function(_, payload) child_output = payload end
FFIUtilStub.isSubProcessDone = function() return true end
FFIUtilStub.getNonBlockingReadSize = function()
    return (child_output and #child_output > 0) and 1 or 0
end
FFIUtilStub.readAllFromFD = function() return child_output end

-- Stands in for UIManager: _runInSubprocess yields between polls and expects
-- something outside the coroutine to resume it.
local function drive(fn)
    local co = coroutine.create(fn)
    local results = table.pack(coroutine.resume(co))
    while coroutine.status(co) == "suspended" do
        results = table.pack(coroutine.resume(co, true))
    end
    assert(results[1], "subprocess driver errored: " .. tostring(results[2]))
    return table.unpack(results, 2, results.n)
end

stub_fork(true)
bridge._in_subprocess = nil
local nested_ok, nested_value = drive(function()
    return bridge:_runInSubprocess(function()
        -- What bridge_api_client does for every non-download request.
        local inner_ok, inner_result = bridge:_runInSubprocess(function()
            return "http-result"
        end)
        return inner_ok and inner_result or nil
    end)
end)
assert(fork_calls == 1,
    "a forked child must run nested subprocess work inline instead of forking again")
assert(nested_ok and nested_value == "http-result",
    "the nested inline result must still reach the caller")

-- A child that dies without writing must not read as success. Returning a bare
-- true handed callers an empty result set, which testConnection renders as its
-- generic "Authentication failed" and the update check as "Version check
-- failed" - a crashed subprocess disguised as a rejected login (#370).
stub_fork(false)
bridge._in_subprocess = nil
local dead_ok, dead_reason = drive(function()
    return bridge:_runInSubprocess(function() return "unreachable" end)
end)
assert(dead_ok == false and dead_reason == "subprocess produced no result",
    "a subprocess that exits without a result must be reported as a failure")
bridge._in_subprocess = nil

-- Session rejection reasons. A session the bridge will never accept used to sit
-- in the queue re-uploading on every wake: after a match was deleted the device
-- logged "Session upload partially accepted: 0 accepted, 3 retained for retry"
-- on every single wake, and the bridge logged "Session upload: book not found"
-- to match, forever.
bridge.session_upload_attempts = {}
bridge.pending_sessions = {}

assert(bridge:_shouldAbandonSession({ session_id = "bad" }, { reason = "invalid_session" }),
    "a malformed session can never become valid and must be abandoned at once")
assert(not bridge:_shouldAbandonSession({ session_id = "t" }, { reason = "record_failed" }),
    "a transient bridge-side failure must stay queued for retry")
assert(not bridge:_shouldAbandonSession({ session_id = "t2" }, nil),
    "a rejection carrying no reason must stay queued for retry")

-- book_not_found recovers if the same file is matched again, so it gets a
-- bounded number of attempts rather than an instant drop or an endless retry.
local gone = { session_id = "gone", abs_id = "ebook-89a7f7b8d25f2391" }
local gone_attempts = 0
while not bridge:_shouldAbandonSession(gone, { reason = "book_not_found" }) do
    gone_attempts = gone_attempts + 1
    assert(gone_attempts < 20, "book_not_found never stopped retrying")
end
gone_attempts = gone_attempts + 1
assert(gone_attempts == 5,
    "book_not_found must be abandoned on the 5th attempt, got " .. tostring(gone_attempts))

-- Attempt counters for sessions that have left the queue must not accumulate.
bridge.pending_sessions = { { session_id = "still-queued" } }
bridge.session_upload_attempts["still-queued"] = 2
bridge:_pruneSessionUploadAttempts()
assert(bridge.session_upload_attempts["still-queued"] == 2,
    "pruning dropped the counter for a session that is still queued")
assert(bridge.session_upload_attempts["gone"] == nil,
    "pruning kept the counter for a session that is no longer queued")

-- Drive the real manifest sweep: failed publication must keep the old file,
-- its progress sidecar and its tracked entry, and leave the revision retryable.
local function file_bytes(path)
    local handle = io.open(path, "rb")
    if not handle then return nil end
    local bytes = handle:read("*a")
    handle:close()
    return bytes
end
local function write_bytes(path, bytes)
    local handle = assert(io.open(path, "wb"))
    handle:write(bytes)
    handle:close()
end
for _, failure in ipairs({ "missing", "empty", "size", "hash", "rename", "rename_eexist", "success" }) do
    local target = settings_dir .. "/replacement.epub"
    local temp_path = target .. ".part"
    local backup_path = target .. ".bak"
    write_bytes(target, "old-good-book")
    local previous = { local_path = target, filename = "replacement.epub", content_hash = "old-good-book" }
    local items = { replacement = previous }
    local sidecar_removed = false
    local saved_revision
    local sync = BridgeSync:new{
        download_dir = settings_dir,
        _ensureDirectory = function() return true end,
        _getStateScalar = function() return "old-revision" end,
        _loadStateItems = function() return items end,
        _buildHashIndex = function() return {} end,
        _fileExists = function(_, path) return file_bytes(path) ~= nil end,
        _calculateBookHash = function(_, path) return file_bytes(path) end,
        _safeRemove = function(_, path) os.remove(path) end,
        _removeTree = function() sidecar_removed = true end,
        _updateCollections = function() end,
        _saveState = function(_, saved, revision) items = saved; saved_revision = revision end,
        logInfo = function() end,
        logWarn = function() end,
        api = {
            getManifest = function() return true, { revision = "new-revision", books = {
                { abs_id = "replacement", filename = "replacement.epub", content_hash = "new-book",
                  size = 8, download_path = "/book" },
            } } end,
            downloadBook = function(_, _, path)
                if failure ~= "missing" then
                    write_bytes(path, failure == "empty" and "" or failure == "size" and "short"
                        or failure == "hash" and "bad-book" or "new-book")
                end
                return true
            end,
        },
    }
    local real_rename = os.rename
    -- The retry after a Windows EEXIST fallback moves the destination aside
    -- and republishes it, so the "previous good file" guarantee only holds
    -- for the FIRST publication attempt on a given book, not every rename
    -- call - the move-aside and restore calls are distinguished by path,
    -- not counted as publication attempts.
    local first_publish_attempt = true
    os.rename = function(src, dst)
        if src == target and dst == backup_path then
            -- The fallback's move-aside: let it actually happen so the
            -- retry (and a possible restore) have a real backup to work
            -- with.
            return real_rename(src, dst)
        end
        if src == backup_path and dst == target then
            -- The fallback's restore after a failed retry.
            return real_rename(src, dst)
        end
        local is_first_call = first_publish_attempt
        first_publish_attempt = false
        if is_first_call then
            assert(file_bytes(dst) == "old-good-book", "publication deleted the previous good file")
        end
        if failure == "rename" then return nil, "No such file or directory" end
        if failure == "rename_eexist" and is_first_call then
            -- os.rename is C rename(): on Windows it fails with EEXIST when
            -- the destination already exists, instead of POSIX's atomic
            -- replace-on-rename.
            return nil, "EEXIST: file already exists"
        end
        -- Model KOReader's POSIX replace on Windows too (or the retry once
        -- the fallback has moved the destination aside).
        os.remove(dst)
        return real_rename(src, dst)
    end
    local ok, result = pcall(function() return sync:_runSync() end)
    os.rename = real_rename
    assert(ok, result)
    if failure == "success" or failure == "rename_eexist" then
        assert(result.downloaded == 1 and result.errors == 0)
        assert(file_bytes(target) == "new-book" and items.replacement.content_hash == "new-book")
        assert(sidecar_removed and saved_revision == "new-revision")
    else
        assert(result.errors == 1 and result.downloaded == 0, failure .. " must fail closed")
        assert(file_bytes(target) == "old-good-book", failure .. " lost the previous book")
        assert(items.replacement == previous and not sidecar_removed, failure .. " lost progress state")
        assert(saved_revision == "", failure .. " must remain retryable")
    end
    assert(file_bytes(target .. ".part") == nil, "partial file leaked")
    assert(file_bytes(backup_path) == nil, "backup file leaked")
    os.remove(target)
end

-- The Kindle downloaded Household Inheritance then deleted it through its old
-- ebook ID. Run the actual forked book-sync entry point and repeat the sync to
-- prove both file retention and SQLite revision persistence.
do
    local filename = "Household Inheritance.epub"
    local target = settings_dir .. "/" .. filename
    local old_id, new_id = "ebook-c84f08fa7c356f9d", "bookorbit:6043"
    local items = { [old_id] = {
        local_path = target, filename = filename, content_hash = "book-bytes",
    } }
    local revision = "old-revision"
    local downloads, deletions = 0, 0
    local SQLite = require("bridge_sqlite_state")
    local original_new = SQLite.new
    SQLite.new = function()
        return {
            init = function() return true end,
            get_setting = function() return revision end,
            get_all_books = function()
                local ids = {}
                for id in pairs(items) do ids[#ids + 1] = id end
                return ids
            end,
            get_all_state_items_for_book = function(_, id) return items[id] end,
            replace_state = function(_, saved, saved_revision)
                items, revision = saved, saved_revision
                return true
            end,
        }
    end
    preload("apps/filemanager/filemanager", empty_module)
    local sync = BridgeSync:new{
        server_url = "http://bridge", username = "reader", key = "test",
        sqlite_available = true, sqlite_state = SQLite:new(),
        state = { readSetting = function() error("book sync read stale LuaSettings") end },
        download_dir = settings_dir, delete_removed_books = true,
        _preflightNetwork = function() return true end,
        _ensureDirectory = function() return true end,
        _buildHashIndex = function() return {} end,
        _fileExists = function(_, path) return file_bytes(path) ~= nil end,
        _calculateBookHash = function(_, path) return file_bytes(path) end,
        _removeTree = function() end,
        _isCurrentDocument = function() return false end,
        _deleteManagedFile = function(_, path)
            deletions = deletions + 1
            os.remove(path)
            return true
        end,
        _updateCollections = function() end,
        _maybeAutoSyncStats = function() end,
        _uploadDeviceLogTail = function() end,
        logInfo = function() end, logWarn = function() end, logErr = function() end,
        api = {
            getManifest = function() return true, { revision = "new-revision", books = {
                { abs_id = new_id, filename = filename, content_hash = "book-bytes",
                  size = 10, download_path = "/book" },
            } } end,
            downloadBook = function(_, _, path)
                downloads = downloads + 1
                write_bytes(path, "book-bytes")
                return true
            end,
        },
    }
    stub_fork(true)
    assert(drive(function() return sync:syncFromBridge(true) end),
        "forked book sync must use its own SQLite connection")
    assert(file_bytes(target) == "book-bytes" and deletions == 0,
        "Household Inheritance was downloaded then deleted through its retired ebook ID")
    assert(items[old_id] == nil and items[new_id] and revision == "new-revision",
        "book sync must persist the replacement ID and revision in SQLite")
    sync._in_subprocess = nil
    assert(drive(function() return sync:syncFromBridge(true) end))
    assert(downloads == 1 and file_bytes(target) == "book-bytes",
        "the next sync must retain Household Inheritance without downloading it again")
    SQLite.new = original_new
    os.remove(target)
end

print("BridgeSync Lua init regression test passed")
