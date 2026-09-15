local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local Dispatcher = require("dispatcher")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local LuaSettings = require("luasettings")
local NetworkMgr = require("ui/network/manager")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")
local lfs = require("libs/libkoreader-lfs")
local bit = require("bit")
local sha2 = require("ffi/sha2")
local md5 = sha2.md5
local FFIUtil = require("ffi/util")
local buffer = require("string.buffer")
local socket = require("socket")
local json = require("json")
local APIClient = require("bridge_api_client")
local BridgeAnnotations = require("bridge_annotations")
local BridgeSweep = require("bridge_sweep")
local BridgeSyncCoordinator = require("bridge_sync_coordinator")
local BridgeStatsBatches = require("bridge_stats_batches")
local BridgeVersion = require("bridge_version")
local ManifestRules = require("bridge_manifest_rules")
local BridgeSqliteState = require("bridge_sqlite_state")
local BridgeSessions = require("bridge_sessions")
local SQ3
do
    local ok, mod = pcall(require, "lua-ljsqlite3/init")
    if ok then
        SQ3 = mod
    end
end
local PathChooser
do
    local ok, mod = pcall(require, "ui/widget/pathchooser")
    if ok then
        PathChooser = mod
    end
end
local DirChooser
do
    local ok, mod = pcall(require, "ui/widget/dirchooser")
    if ok then
        DirChooser = mod
    end
end

local _ = require("gettext")
local T = require("ffi/util").template
local STATS_UPLOAD_BATCH = 3000
-- A book_not_found rejection usually means the match was deleted on the bridge,
-- but it recovers if the same file is matched again -- so retry a few times
-- before giving up rather than either looping forever or dropping instantly.
local SESSION_MAX_BOOK_NOT_FOUND_ATTEMPTS = 5
local SETTINGS_VERSION = 1
local DEVICE_LOG_UPLOAD_MAX_BYTES = 24 * 1024
local DEVICE_LOG_MAX_BYTES = 512 * 1024

-- Forked children still alive, keyed by pid. Suspend has to be able to kill
-- work started by any plugin instance, not just the one handling the event.
local live_subprocess_pids = {}

-- One queue for the whole app. KOReader builds a BridgeSync for the file
-- manager and for every ReaderUI, and overlapping instances each ran their own
-- copy of the after-wake job set; a shared coordinator collapses those by
-- family instead of replaying them.
local shared_coordinator = nil

local BridgeSync = WidgetContainer:extend{
    name = "bridgesync",
    is_doc_only = false,
}

function BridgeSync:onDispatcherRegisterActions()
    Dispatcher:registerAction("bridgesync_sync_books", {
        category = "none",
        event = "BridgeSyncSyncBooks",
        title = _("Bridge Sync: Sync books"),
        general = true,
    })
    Dispatcher:registerAction("bridgesync_sync_stats", {
        category = "none",
        event = "BridgeSyncSyncStats",
        title = _("Bridge Sync: Sync reading stats"),
        general = true,
    })
    Dispatcher:registerAction("bridgesync_sync_annotations", {
        category = "none",
        event = "BridgeSyncSyncAnnotations",
        title = _("Bridge Sync: Sync highlights"),
        general = true,
    })
    Dispatcher:registerAction("bridgesync_sweep_annotations", {
        category = "none",
        event = "BridgeSyncSweepAnnotations",
        title = _("Bridge Sync: Sweep all highlights"),
        general = true,
    })
end

function BridgeSync:init()
    self:onDispatcherRegisterActions()
    self.settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/bridge_sync.lua")
    self.state = LuaSettings:open(DataStorage:getSettingsDir() .. "/bridge_sync_state.lua")
    self.log_path = DataStorage:getSettingsDir() .. "/bridge_sync.log"

    -- Initialize SQLite state manager
    self.sqlite_state = BridgeSqliteState:new()
    local sqlite_available = false
    if self.sqlite_state:is_available() then
        local ok, err = self.sqlite_state:init()
        if ok then
            sqlite_available = true
            self:logInfo("SQLite state manager initialized")

            -- Migrate from LuaSettings if first run. A failed migration must
            -- not leave us reading an empty database, so fall back to
            -- LuaSettings for this run instead.
            local migration_done = self.sqlite_state:get_setting("migration_done", false)
            if not migration_done then
                self:logInfo("Migrating data from LuaSettings to SQLite...")
                local migrate_ok, migrate_err = self.sqlite_state:migrate_from_luasettings(self.state, self.settings)
                if migrate_ok then
                    self.sqlite_state:set_setting("migration_done", true)
                    self:logInfo("Migration completed successfully")
                else
                    self:logWarn("Migration failed, staying on LuaSettings:", tostring(migrate_err))
                    self.sqlite_state:close()
                    sqlite_available = false
                end
            end
        else
            self:logWarn("SQLite initialization failed:", tostring(err))
        end
    else
        self:logInfo("SQLite not available, falling back to LuaSettings")
    end

    self.sqlite_available = sqlite_available
    if self.sqlite_available then
        self.sqlite_state:prune_uploaded_sessions(os.time() - 7 * 24 * 3600)
    end

    -- Unified getter for settings (SQLite first, fallback to LuaSettings).
    -- Must preserve stored `false` values - never collapse them into default.
    local function get_setting(key, default)
        if self.sqlite_available then
            return self.sqlite_state:get_setting(key, default)
        end
        local value = self.settings:readSetting(key)
        if value == nil then
            return default
        end
        return value
    end

    self.server_url = get_setting("server_url") or ""
    self.username = get_setting("username") or ""
    self.key = get_setting("key") or ""
    self.download_dir = get_setting("download_dir") or self:_detectDefaultDownloadDir()
    self.is_enabled = get_setting("is_enabled") or false
    self.auto_sync_on_resume = get_setting("auto_sync_on_resume") or false
    self.auto_sync_on_network = get_setting("auto_sync_on_network") or false
    local auto_sync_on_close = get_setting("auto_sync_on_close")
    if auto_sync_on_close == nil then
        self.auto_sync_on_close = true
    else
        self.auto_sync_on_close = auto_sync_on_close
    end
    self.delete_removed_books = get_setting("delete_removed_books") or false
    self.max_downloads_per_sync = tonumber(get_setting("max_downloads_per_sync")) or 0
    self.manual_only = get_setting("manual_only") or false
    local do_not_sync_while_book_open = get_setting("do_not_sync_while_book_open")
    if do_not_sync_while_book_open == nil then
        self.do_not_sync_while_book_open = true
    else
        self.do_not_sync_while_book_open = do_not_sync_while_book_open
    end
    self.wake_sync_delay_seconds = tonumber(get_setting("wake_sync_delay_seconds")) or 30

    -- Reading session tracking
    local session_tracking = get_setting("session_tracking_enabled")
    if session_tracking == nil then
        self.session_tracking_enabled = true
    else
        self.session_tracking_enabled = session_tracking
    end
    self.min_session_duration = tonumber(get_setting("min_session_duration")) or 30
    self.session_merge_threshold = tonumber(get_setting("session_merge_threshold")) or 300
    local auto_sync_stats = get_setting("auto_sync_stats")
    if auto_sync_stats == nil then
        self.auto_sync_stats = true
    else
        self.auto_sync_stats = auto_sync_stats
    end
    local annotation_sync = get_setting("annotation_sync_enabled")
    if annotation_sync == nil then
        self.annotation_sync_enabled = true
    else
        self.annotation_sync_enabled = annotation_sync
    end

    local settings_version = tonumber(get_setting("settings_version")) or 0
    if settings_version < SETTINGS_VERSION then
        self:_saveSetting("settings_version", SETTINGS_VERSION)
    end
    self.current_session = nil
    
    -- Load pending sessions (SQLite first, fallback to LuaSettings)
    if self.sqlite_available then
        self.pending_sessions = self.sqlite_state:get_pending_sessions(nil, false) or {}
    else
        self.pending_sessions = self.state:readSetting("pending_sessions") or {}
    end
    self.pending_annotation_closes = self:_getStateScalarJSON("pending_annotation_closes", {}) or {}
    self.session_upload_attempts = self:_getStateScalarJSON("session_upload_attempts", {}) or {}

    self.sync_in_progress = false
    self.last_auto_sync_time = 0
    self.last_stats_sync_time = 0
    self.stats_sync_in_flight = false
    self.stats_sync_scheduled = false
    self.annotation_sync_scheduled = false
    self.needs_wake_sync = false
    self.sync_scheduled = false
    self.close_book_sync_scheduled = false
    self.scheduled_tasks = {}
    self.api = APIClient:new()
    self.api:init(self.server_url, self.username, self.key, function(level, message)
        self:_appendLog(level, message)
    end, function(task)
        return self:_runInSubprocess(task)
    end)
    if not shared_coordinator then
        shared_coordinator = BridgeSyncCoordinator:new()
    end
    self.sync_coordinator = shared_coordinator

    self.ui.menu:registerToMainMenu(self)
    self:_scheduleTask("plugin_update", 10, function()
        self:_maybeCheckForPluginUpdate()
    end)
end

-- Helper method to save a setting (SQLite first, fallback to LuaSettings)
function BridgeSync:_saveSetting(key, value)
    if self._state_unavailable then return false end
    if self.sqlite_available then
        return self.sqlite_state:set_setting(key, value)
    else
        self.settings:saveSetting(key, value)
        self.settings:flush()
        return true
    end
end

-- Generic scalar state accessor (wraps a single key-value in plugin_settings for SQLite,
-- or the flat LuaSettings file for legacy mode)
function BridgeSync:_getStateScalar(key, default)
    if self.sqlite_available then
        return self.sqlite_state:get_setting(key, default)
    end
    local value = self.state:readSetting(key)
    if value == nil then
        return default
    end
    return value
end

function BridgeSync:_saveStateScalar(key, value)
    if self._state_unavailable then return false end
    if self.sqlite_available then
        return self.sqlite_state:set_setting(key, value)
    else
        self.state:saveSetting(key, value)
        self.state:flush()
        return true
    end
end

-- JSON scalar state helpers (for storing/reading table values as JSON strings in SQLite)
function BridgeSync:_saveStateScalarJSON(key, value)
    if self._state_unavailable then return false end
    if self.sqlite_available then
        return self.sqlite_state:set_setting(key, json.encode(value))
    else
        self.state:saveSetting(key, value)
        self.state:flush()
        return true
    end
end

function BridgeSync:_getStateScalarJSON(key, default)
    if self.sqlite_available then
        local raw = self.sqlite_state:get_setting(key, nil)
        if raw then
            local ok, result = pcall(json.decode, raw)
            if ok then
                return result
            end
        end
        return default
    else
        return self.state:readSetting(key) or default
    end
end

-- ============================================================
-- Differential sync fingerprint helpers
-- These track when data was last synced and with what
-- fingerprint, so we can skip redundant syncs for unchanged
-- data.  Wraps plugin_sync_timestamps (SQLite) with a
-- LuaSettings fallback.
-- ============================================================

-- Record a sync fingerprint for a (service, sync_type, abs_id) tuple.
-- @string service  e.g. "bridge", "abs", "grimmory"
-- @string sync_type e.g. "statistics", "annotations", "manifest"
-- @string abs_id    book identifer or "*" for global
-- @string fingerprint opaque string that changes when data does
function BridgeSync:_recordSyncFingerprint(service, sync_type, abs_id, fingerprint)
    if self.sqlite_available then
        self.sqlite_state:set_sync_timestamp(abs_id or "*", service, sync_type, fingerprint)
    else
        local key = "sync_fp:" .. tostring(service) .. ":" .. tostring(sync_type) .. ":" .. tostring(abs_id or "*")
        self:_saveStateScalarJSON(key, { fp = fingerprint, at = os.time() })
    end
end

-- Retrieve the last stored sync fingerprint.
-- @return string|nil
function BridgeSync:_getSyncFingerprint(service, sync_type, abs_id)
    if self.sqlite_available then
        local ts = self.sqlite_state:get_sync_timestamp(abs_id or "*", service, sync_type)
        if ts then
            return ts.sync_hash
        end
        return nil
    else
        local key = "sync_fp:" .. tostring(service) .. ":" .. tostring(sync_type) .. ":" .. tostring(abs_id or "*")
        local stored = self:_getStateScalarJSON(key)
        if stored then
            return stored.fp
        end
        return nil
    end
end

-- True when the current fingerprint differs from the stored one
-- (or nothing has ever been stored).  Callers skip the sync when
-- this returns false.
-- @return boolean  true = data changed, sync needed
function BridgeSync:_fingerprintChanged(service, sync_type, abs_id, current_fingerprint)
    local stored = self:_getSyncFingerprint(service, sync_type, abs_id)
    return stored == nil or stored ~= current_fingerprint
end

-- Persist the pending-session list. In SQLite mode rows are written
-- individually as sessions are created/merged/uploaded, so this is a no-op
-- there; the LuaSettings backend stores the whole list in one key.
function BridgeSync:_savePendingSessions(sessions)
    if not self.sqlite_available then
        self.state:saveSetting("pending_sessions", sessions)
        self.state:flush()
    end
    return true
end

function BridgeSync:_appendLog(level, message)
    local line = os.date("%Y-%m-%d %H:%M:%S") .. " [" .. tostring(level or "info") .. "] " .. tostring(message or "") .. "\n"
    local current_size = tonumber((lfs.attributes(self.log_path, "size"))) or 0
    if current_size + #line > DEVICE_LOG_MAX_BYTES then
        os.remove(self.log_path .. ".1")
        os.rename(self.log_path, self.log_path .. ".1")
    end
    local handle = io.open(self.log_path, "a")
    if handle then
        handle:write(line)
        handle:close()
    end
end

function BridgeSync:logInfo(...)
    logger.info("Bridge Sync:", ...)
    self:_appendLog("info", table.concat({...}, " "))
end

function BridgeSync:logWarn(...)
    logger.warn("Bridge Sync:", ...)
    self:_appendLog("warn", table.concat({...}, " "))
end

function BridgeSync:logErr(...)
    logger.err("Bridge Sync:", ...)
    self:_appendLog("error", table.concat({...}, " "))
end

function BridgeSync:_pluginVersion()
    if not self.path or self.path == "" then return "unknown" end
    local chunk = loadfile(self.path .. "/_meta.lua")
    if not chunk then return "unknown" end
    local ok, meta = pcall(chunk)
    if ok and type(meta) == "table" and meta.version then
        return tostring(meta.version)
    end
    return "unknown"
end

function BridgeSync:_readDeviceLogChunk()
    local handle = io.open(self.log_path, "rb")
    if not handle then return {}, nil end

    local size = handle:seek("end") or 0
    local saved_offset = tonumber(self:_getStateScalar("device_log_upload_offset"))
    local offset = saved_offset
    if offset == nil or offset < 0 or offset > size then
        offset = math.max(0, size - DEVICE_LOG_UPLOAD_MAX_BYTES)
    end

    handle:seek("set", offset)
    local chunk = handle:read(DEVICE_LOG_UPLOAD_MAX_BYTES) or ""
    local next_offset = handle:seek() or (offset + #chunk)
    handle:close()

    local lines = {}
    for line in chunk:gmatch("[^\r\n]+") do
        lines[#lines + 1] = line
    end
    return lines, next_offset
end

-- Best-effort telemetry: ship only the unread, bounded BridgeSync log tail.
-- The saved byte offset advances only after a successful bridge acknowledgement,
-- so network/server failures are retried after the next book or session sync.
function BridgeSync:_uploadDeviceLogTail(operation, status)
    if not self.api or not self.api.uploadClientLogs then return false end
    if not self.server_url or self.server_url == "" then return false end

    local lines, next_offset = self:_readDeviceLogChunk()
    local device, device_id = self:_currentDeviceIdentity()
    local payload = {
        operation = tostring(operation or "unknown"),
        status = tostring(status or "unknown"),
        plugin_version = self:_pluginVersion(),
        device = device,
        device_id = device_id,
        lines = lines,
    }

    -- Do not append the telemetry POST itself to bridge_sync.log, which would
    -- otherwise create one new line to upload after every successful upload.
    local original_callback = self.api.log_callback
    self.api.log_callback = nil
    local call_ok, ok, code, body = pcall(self.api.uploadClientLogs, self.api, payload)
    self.api.log_callback = original_callback

    if call_ok and ok then
        if next_offset ~= nil then
            self:_saveStateScalar("device_log_upload_offset", next_offset)
        end
        return true
    end

    local reason = call_ok and (body or code or "request failed") or ok
    self:_appendLog("warn", "Device log telemetry upload failed: " .. tostring(reason or "unknown error"))
    return false
end

function BridgeSync:_detectDefaultDownloadDir()
    -- Known device storage roots, checked in order.
    local roots = {
        "/mnt/onboard",         -- Kobo
        "/mnt/us",              -- Kindle
        "/mnt/ext1",            -- PocketBook
        "/storage/emulated/0",  -- Android
        "/sdcard",              -- Android (legacy symlink)
    }
    for _, root in ipairs(roots) do
        if lfs.attributes(root, "mode") == "directory" then
            return root .. "/Books/BridgeManaged"
        end
    end

    -- Desktop/emulator builds have none of the above. Prefer KOReader's own
    -- library folder, then its data dir; never "/Books", which is at the
    -- filesystem root and unwritable for a normal user.
    if G_reader_settings and G_reader_settings.readSetting then
        local home = G_reader_settings:readSetting("home_dir")
        if type(home) == "string" and lfs.attributes(home, "mode") == "directory" then
            return self:_normalizePath(home) .. "/BridgeManaged"
        end
    end
    return self:_normalizePath(DataStorage:getDataDir()) .. "/Books/BridgeManaged"
end

function BridgeSync:_saveSettings()
    self:_saveSetting("settings_version", SETTINGS_VERSION)
    self:_saveSetting("server_url", self.server_url)
    self:_saveSetting("username", self.username)
    self:_saveSetting("key", self.key)
    self:_saveSetting("download_dir", self.download_dir)
    self:_saveSetting("is_enabled", self.is_enabled)
    self:_saveSetting("auto_sync_on_resume", self.auto_sync_on_resume)
    self:_saveSetting("auto_sync_on_network", self.auto_sync_on_network)
    self:_saveSetting("auto_sync_on_close", self.auto_sync_on_close)
    self:_saveSetting("delete_removed_books", self.delete_removed_books)
    self:_saveSetting("max_downloads_per_sync", self.max_downloads_per_sync)
    self:_saveSetting("manual_only", self.manual_only)
    self:_saveSetting("do_not_sync_while_book_open", self.do_not_sync_while_book_open)
    self:_saveSetting("wake_sync_delay_seconds", self.wake_sync_delay_seconds)
    self:_saveSetting("session_tracking_enabled", self.session_tracking_enabled)
    self:_saveSetting("min_session_duration", self.min_session_duration)
    self:_saveSetting("auto_sync_stats", self.auto_sync_stats)
    self:_saveSetting("annotation_sync_enabled", self.annotation_sync_enabled)
    
    -- If using LuaSettings, flush both settings and state files
    if not self.sqlite_available then
        self.settings:flush()
    end
    
    self.api:init(self.server_url, self.username, self.key, function(level, message)
        self:_appendLog(level, message)
    end, function(task)
        return self:_runInSubprocess(task)
    end)
end

function BridgeSync:_extractHost()
    return tostring(self.server_url or ""):match("^https?://([^/%:]+)")
end

function BridgeSync:_preflightNetwork(allow_dns_retry)
    if not NetworkMgr:isConnected() then
        return false, _("WiFi is not connected")
    end

    local host = self:_extractHost()
    if not host or host == "" then
        return false, _("Server URL is invalid")
    end

    if host:match("^%d+%.%d+%.%d+%.%d+$") then
        return true
    end

    local resolved_ip = socket.dns.toip(host)
    if not resolved_ip then
        if allow_dns_retry then
            -- Right after wake DNS can miss transiently; let the request proceed and rely on
            -- the API client's connection-failure retries instead of aborting the whole sync.
            self:logWarn(T(_("DNS lookup failed for %1; proceeding, will retry"), host))
            return true
        end
        return false, T(_("DNS lookup failed for %1"), host)
    end

    return true
end

function BridgeSync:_loadStateItems()
    if self.sqlite_available then
        -- Load all state items from SQLite and convert to the legacy format
        local all_items = {}
        local books = self.sqlite_state:get_all_books()
        for _, abs_id in ipairs(books) do
            local book_items = self.sqlite_state:get_all_state_items_for_book(abs_id)
            if book_items and next(book_items) then
                all_items[abs_id] = book_items
            end
        end
        return all_items
    else
        return self.state:readSetting("items") or {}
    end
end

function BridgeSync:_saveState(items, revision)
    if self.sqlite_available then
        -- Full replace, matching the LuaSettings semantics: books that left
        -- the manifest must not linger as stale state rows.
        self.sqlite_state:replace_state(items or {}, revision or "")
    else
        self.state:saveSetting("items", items or {})
        self.state:saveSetting("revision", revision or "")
        self.state:flush()
    end
end

function BridgeSync:_showMessage(text, timeout)
    UIManager:show(InfoMessage:new{
        text = text,
        timeout = timeout or 3,
    })
end

function BridgeSync:_normalizePath(path)
    local normalized = tostring(path or ""):gsub("\\", "/"):gsub("/+$", "")
    if normalized == "" then
        return ""
    end
    return normalized
end

function BridgeSync:_shouldAvoidAutoSyncWhileReading()
    if not self.do_not_sync_while_book_open then
        return false
    end
    return self:_currentDocumentPath() ~= nil
end

function BridgeSync:_currentFileManagerPath()
    local FileManager = require("apps/filemanager/filemanager")
    local instance = FileManager and FileManager.instance or nil
    if not instance or not instance.file_chooser then
        return nil
    end

    local current_path = instance.file_chooser.path
    if type(current_path) ~= "string" or current_path == "" then
        return nil
    end
    return self:_normalizePath(current_path)
end

function BridgeSync:_refreshMenu(touchmenu_instance)
    if touchmenu_instance and touchmenu_instance.updateItems then
        touchmenu_instance:updateItems()
    end
end

function BridgeSync:_refreshCollectionsUI()
    local ok_read, ReadCollection = pcall(require, "readcollection")
    if not ok_read or not ReadCollection then
        return
    end

    local seen = {}
    local instances = {}
    local function addInstance(ui)
        if ui and not seen[ui] then
            seen[ui] = true
            table.insert(instances, ui)
        end
    end

    local ok_fm, FileManager = pcall(require, "apps/filemanager/filemanager")
    if ok_fm and FileManager and FileManager.instance then
        addInstance(FileManager.instance)
    end

    local ok_reader, ReaderUI = pcall(require, "apps/reader/readerui")
    if ok_reader and ReaderUI and ReaderUI.instance then
        addInstance(ReaderUI.instance)
    end

    for _, ui in ipairs(instances) do
        local collections = ui.collections
        if collections then
            if collections.coll_list and collections.updateCollListItemTable then
                collections:updateCollListItemTable(true)
            end
            if collections.booklist_menu and collections.updateItemTable then
                local current_collection = collections.booklist_menu.path
                if ReadCollection.coll and ReadCollection.coll[current_collection] then
                    collections:updateItemTable()
                elseif collections.booklist_menu.close_callback then
                    collections.booklist_menu.close_callback()
                end
            end
            if collections.coll_folder_list and collections.updateCollFolderListItemTable then
                collections:updateCollFolderListItemTable()
            end
        end
    end
end

function BridgeSync:_showManagedFolderChooser(touchmenu_instance)
    local start_path = self.download_dir
    if lfs.attributes(start_path, "mode") ~= "directory" then
        start_path = self:_detectDefaultDownloadDir()
    end

    if PathChooser then
        local chooser
        chooser = PathChooser:new{
            select_directory = true,
            select_file = false,
            show_files = false,
            path = start_path,
            title = _("Managed Folder"),
            onConfirm = function(path)
                local selected = self:_normalizePath(path)
                if selected == "" then
                    return
                end
                self.download_dir = selected
                self:_saveSettings()
                self:_refreshMenu(touchmenu_instance)
                self:_showMessage(T(_("Managed Folder set to %1"), self.download_dir), 3)
            end,
        }
        UIManager:show(chooser)
        return
    end

    if DirChooser then
        local chooser
        chooser = DirChooser:new{
            path = start_path,
            title = _("Managed Folder"),
            onConfirm = function(path)
                local selected = self:_normalizePath(path)
                if selected == "" then
                    return
                end
                self.download_dir = selected
                self:_saveSettings()
                self:_refreshMenu(touchmenu_instance)
                self:_showMessage(T(_("Managed Folder set to %1"), self.download_dir), 3)
            end,
        }
        UIManager:show(chooser)
        return
    end

    self:_showMessage(_("Managed Folder picker is not available on this KOReader build"), 4)
end

function BridgeSync:_runInSubprocess(task, opts)
    opts = opts or {}

    -- Forked child: it must never fork again. The child inherits the running
    -- coroutine, so a nested call would fork a grandchild and then yield the
    -- child back into a copy of the parent's UIManager loop, leaving a second
    -- instance of the app running against the same screen and input devices.
    if self._in_subprocess then
        return true, task()
    end

    -- A fork costs a copy-on-write image of the whole reader (~100 MB on a
    -- Kindle), which is why short tasks ask to stay on the UI loop instead.
    if opts.inline then
        return true, task()
    end

    local co, is_main = coroutine.running()
    if not co or is_main then
        return true, task()
    end

    local needs_sqlite = opts.needs_sqlite == true
    local pid, parent_read_fd = FFIUtil.runInSubProcess(function(_, child_write_fd)
        self._in_subprocess = true
        if needs_sqlite and self.sqlite_available then
            -- Forked child: a SQLite handle must never be shared across a
            -- fork, so replace the inherited one with the child's own.
            self.sqlite_state = BridgeSqliteState:new()
            if not self.sqlite_state:init() then
                self.sqlite_available = false
            end
        else
            -- This task needs no database. Opening one here is what made every
            -- forked request contend with the parent for the same file and log
            -- "database is locked", so drop the inherited handle instead and
            -- refuse state writes, which could never reach the parent anyway.
            self.sqlite_state = nil
            self.sqlite_available = false
            self._state_unavailable = true
        end
        local output_str = ""
        local results = table.pack(task())
        local ok, serialized = pcall(buffer.encode, results)
        if ok then
            output_str = serialized
        else
            print("Bridge Sync subprocess serialize failed:", tostring(serialized))
        end
        FFIUtil.writeToFD(child_write_fd, output_str, true)
    end, true)

    if not pid then
        return false, parent_read_fd or "failed to start subprocess"
    end

    live_subprocess_pids[pid] = true

    local check_interval_sec = 0.125
    local check_num = 0
    local ret_values

    while true do
        check_num = check_num + 1
        if check_interval_sec < 1 and check_num % 10 == 0 then
            check_interval_sec = math.min(check_interval_sec * 2, 1)
        end

        local go_on_func = function()
            coroutine.resume(co, true)
        end
        UIManager:scheduleIn(check_interval_sec, go_on_func)
        coroutine.yield()

        local subprocess_done = FFIUtil.isSubProcessDone(pid)
        local stuff_to_read = parent_read_fd and FFIUtil.getNonBlockingReadSize(parent_read_fd) ~= 0
        if subprocess_done or stuff_to_read then
            if stuff_to_read then
                local ret_str = FFIUtil.readAllFromFD(parent_read_fd)
                local ok, decoded = pcall(buffer.decode, ret_str)
                if ok and decoded then
                    ret_values = decoded
                else
                    live_subprocess_pids[pid] = nil
                    return false, decoded or "malformed subprocess result"
                end
                if not subprocess_done then
                    local collect_and_clean
                    collect_and_clean = function()
                        if FFIUtil.isSubProcessDone(pid) then
                            logger.dbg("Bridge Sync subprocess collected")
                            live_subprocess_pids[pid] = nil
                        else
                            UIManager:scheduleIn(1, collect_and_clean)
                        end
                    end
                    UIManager:scheduleIn(1, collect_and_clean)
                end
            else
                FFIUtil.readAllFromFD(parent_read_fd)
            end
            break
        end
    end

    if FFIUtil.isSubProcessDone(pid) then
        live_subprocess_pids[pid] = nil
    end

    if ret_values then
        return true, table.unpack(ret_values, 1, ret_values.n or #ret_values)
    end
    -- The child exited without writing a result. Reporting success here hands
    -- the caller an empty result set, which it renders as its generic failure
    -- text, so a crashed subprocess reads as a rejected login.
    return false, "subprocess produced no result"
end

function BridgeSync:_promptForSetting(title, current_value, hint, setter, is_password, after_save)
    local dialog
    dialog = InputDialog:new{
        title = title,
        input = current_value or "",
        input_hint = hint or "",
        text_type = is_password and "password" or nil,
        buttons = {
            {
                {
                    text = _("Cancel"),
                    callback = function()
                        UIManager:close(dialog)
                    end,
                },
                {
                    text = _("Save"),
                    is_enter_default = true,
                    callback = function()
                        setter(dialog:getInputText() or "")
                        UIManager:close(dialog)
                        if after_save then
                            after_save()
                        end
                    end,
                },
            },
        },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function BridgeSync:_ensureDirectory(path)
    local normalized = tostring(path or "")
    if normalized == "" then
        return false, "no folder is configured"
    end

    if lfs.attributes(normalized, "mode") == "directory" then
        return true
    end

    local partial = ""
    for segment in normalized:gmatch("[^/]+") do
        if partial == "" then
            partial = normalized:sub(1, 1) == "/" and "/" .. segment or segment
        else
            partial = partial .. "/" .. segment
        end
        local mode = lfs.attributes(partial, "mode")
        if mode == nil then
            local ok, mkdir_err = lfs.mkdir(partial)
            if not ok and lfs.attributes(partial, "mode") ~= "directory" then
                return false, partial .. ": " .. tostring(mkdir_err or "mkdir failed")
            end
        elseif mode ~= "directory" then
            return false, partial .. " already exists and is not a folder"
        end
    end

    if lfs.attributes(normalized, "mode") ~= "directory" then
        return false, normalized .. " could not be created"
    end
    return true
end

function BridgeSync:_isCooldownActive()
    if not self.last_auto_sync_time then
        return false
    end
    return (os.time() - self.last_auto_sync_time) < 300
end

-- Whether a wake/network event has anything to do, so we don't schedule a WiFi-polling
-- job (and its "waiting for WiFi" logging) when there's nothing to sync.
function BridgeSync:_hasWakeWork()
    if not self.is_enabled then
        return false
    end
    if #self.pending_sessions > 0 then
        return true
    end
    if #self.pending_annotation_closes > 0 then
        return true
    end
    if self.session_tracking_enabled and self.auto_sync_stats
        and (os.time() - (self.last_stats_sync_time or 0)) >= 300 then
        return true
    end
    if not self.manual_only and not self:_isCooldownActive()
        and (self.auto_sync_on_resume or self.auto_sync_on_network or self.needs_wake_sync) then
        return true
    end
    return false
end

-- Runs once WiFi is confirmed connected (called by _scheduleSync's poll loop). Sessions and
-- stats are flushed unconditionally (safe while reading); the manifest/book pull stays
-- deferred while a document is open and respects the auto-sync toggles + cooldown.
function BridgeSync:_runScheduledWork(silent)
    self:_maybeUploadPendingSessions("wake")
    self:_maybeUploadPendingAnnotationCloses("wake")
    self:_maybeAutoSyncStats("wake")

    if self.manual_only then
        return
    end
    if not (self.auto_sync_on_resume or self.auto_sync_on_network or self.needs_wake_sync) then
        return
    end
    if self:_isCooldownActive() then
        return
    end
    if self:_shouldAvoidAutoSyncWhileReading() then
        self.needs_wake_sync = true
        self:logInfo("Deferring book sync while a document is open")
        return
    end
    self:_scheduleAutoBookSync("wake", 1, silent == nil and true or silent)
end

function BridgeSync:_submitSyncJob(family, label, source, priority, silent, action)
    local result = self.sync_coordinator:submit({
        owner = self,
        family = family,
        label = label,
        source = source,
        priority = priority,
        run = function(done)
            Trapper:wrap(function()
                -- Suspend or a ReaderUI teardown can flag this job between the
                -- coordinator starting it and Trapper actually entering it.
                if self.sync_coordinator:isActiveCancelled() then
                    self:logInfo(label, "cancelled before it ran")
                    done()
                    return
                end
                local ok, outcome = pcall(action)
                if not ok then
                    self:logErr(label, "failed:", tostring(outcome))
                end
                if not self.sync_coordinator:isActiveCancelled() then
                    self:_saveStateScalarJSON("last_sync_job", {
                        family = family,
                        label = label,
                        source = source,
                        at = os.time(),
                        success = ok and outcome ~= false,
                        error = not ok and tostring(outcome) or nil,
                    })
                end
                done()
            end)
        end,
        on_error = function(err)
            self:logErr(label, "could not start:", tostring(err))
        end,
    })
    if not silent and (result == "queued" or result == "replaced" or result == "kept") then
        self:_showMessage(T(_("%1 queued"), label), 2)
    end
    return result
end

function BridgeSync:_syncStatusSummary()
    local status = self.sync_coordinator:status()
    local last = self:_getStateScalarJSON("last_sync_job", {})
    local current = status.current and status.current.label or _("Idle")
    local last_text = _("None")
    if last.label then
        last_text = T(
            _("%1 at %2 (%3)"),
            tostring(last.label),
            os.date("%Y-%m-%d %H:%M", tonumber(last.at) or 0),
            last.success and _("success") or _("failed")
        )
    end
    return T(
        _("Current: %1\nQueued: %2\nLast: %3"),
        current,
        tonumber(status.pending_count) or 0,
        last_text
    )
end

function BridgeSync:_cancelTask(name)
    local callback = self.scheduled_tasks and self.scheduled_tasks[name]
    if callback then
        UIManager:unschedule(callback)
        self.scheduled_tasks[name] = nil
    end
end

function BridgeSync:_scheduleTask(name, delay_seconds, callback)
    self.scheduled_tasks = self.scheduled_tasks or {}
    self:_cancelTask(name)
    local scheduled
    scheduled = function()
        if self.scheduled_tasks[name] ~= scheduled then return end
        self.scheduled_tasks[name] = nil
        callback()
    end
    self.scheduled_tasks[name] = scheduled
    UIManager:scheduleIn(delay_seconds, scheduled)
end

function BridgeSync:_cancelAutomaticTasks()
    if self.close_book_sync_scheduled then self.needs_wake_sync = true end
    for _, name in ipairs({
        "wake_sync", "book_sync", "annotation_sync", "stats_sync",
        "close_annotations", "plugin_update",
    }) do
        self:_cancelTask(name)
    end
    self.sync_scheduled = false
    self.close_book_sync_scheduled = false
    self.annotation_sync_scheduled = false
    self.stats_sync_scheduled = false
end

function BridgeSync:_scheduleSync(delay_seconds, silent, retries_left)
    if self.sync_scheduled then
        return
    end

    self.sync_scheduled = true
    self:_scheduleTask("wake_sync", delay_seconds or 10, function()
        self.sync_scheduled = false
        if not self.is_enabled then
            return
        end
        if not NetworkMgr:isConnected() then
            -- WiFi isn't up yet (common right after wake): poll instead of giving up so the
            -- sync fires as soon as it connects, independent of the onNetworkConnected event.
            local remaining = retries_left or 12  -- ~12 * 10s ≈ 2 min grace for WiFi
            if remaining > 0 then
                self:logInfo("Auto-sync waiting for WiFi; will retry")
                self:_scheduleSync(10, silent, remaining - 1)
            else
                self:logInfo("Auto-sync gave up waiting for WiFi")
            end
            return  -- needs_wake_sync stays true as a backup for onNetworkConnected
        end
        self:_runScheduledWork(silent)
    end)
end

function BridgeSync:_scheduleAutoBookSync(reason, delay_seconds, silent, retries_left)
    if not self.is_enabled or self.manual_only then
        return
    end
    if self.close_book_sync_scheduled then
        self:logInfo("Book sync already scheduled; skipping duplicate", tostring(reason or "auto"))
        return
    end
    if self:_isCooldownActive() then
        self:logInfo("Book sync skipped by cooldown after", tostring(reason or "auto"))
        return
    end

    self.close_book_sync_scheduled = true
    self:_scheduleTask("book_sync", delay_seconds or 1, function()
        self.close_book_sync_scheduled = false
        if not self.is_enabled or self.manual_only then
            return
        end
        if self:_isCooldownActive() then
            self:logInfo("Book sync skipped by cooldown after", tostring(reason or "auto"))
            return
        end
        if not NetworkMgr:isConnected() then
            self.needs_wake_sync = true
            self:logInfo("Book sync after", tostring(reason or "auto"), "waiting for WiFi")
            return
        end
        if self:_shouldAvoidAutoSyncWhileReading() then
            self.needs_wake_sync = true
            self:logInfo("Deferring book sync after", tostring(reason or "auto"), "while a document is open")
            return
        end
        self.needs_wake_sync = false
        self:_submitSyncJob(
            "books",
            _("Book sync"),
            reason or "auto",
            reason == "close"
                and BridgeSyncCoordinator.PRIORITY.lifecycle
                or BridgeSyncCoordinator.PRIORITY.automatic,
            true,
            function()
            self.last_auto_sync_time = os.time()
            local ok = self:syncFromBridge(silent == nil and true or silent)
            if not ok then
                self.needs_wake_sync = true
                self:logWarn("Book sync after", tostring(reason or "auto"), "did not complete")
            end
            return ok
            end
        )
    end)
end

function BridgeSync:_scheduleBookSyncAfterClose(delay_seconds)
    if not self.is_enabled or self.manual_only or not self.auto_sync_on_close then
        return
    end
    self:_scheduleAutoBookSync("close", delay_seconds or 5, true)
end

function BridgeSync:_maybeUploadPendingSessions(reason)
    if #self.pending_sessions == 0 then
        return false
    end
    if not NetworkMgr:isConnected() then
        if reason then
            self:logInfo("Pending sessions still queued after", reason, "- waiting for WiFi")
        end
        return false
    end

    if reason then
        self:logInfo("Uploading pending sessions after", reason)
    end
    self:_submitSyncJob(
        "sessions", _("Reading session upload"), reason or "automatic",
        BridgeSyncCoordinator.PRIORITY.automatic, true,
        function()
            return self:_uploadSessions()
        end
    )
    return true
end

function BridgeSync:_scheduleAutoAnnotationSync(reason, delay_seconds, silent, retries_left)
    if not self.is_enabled or not self.annotation_sync_enabled then
        return false
    end
    if self.annotation_sync_scheduled then
        self:logInfo("Highlight sync already scheduled; skipping duplicate", tostring(reason or "auto"))
        return true
    end

    self.annotation_sync_scheduled = true
    self:_scheduleTask("annotation_sync", delay_seconds or 1, function()
        self.annotation_sync_scheduled = false
        if not self.is_enabled or not self.annotation_sync_enabled then
            return
        end
        if not NetworkMgr:isConnected() then
            self:logInfo("Highlight sync after", tostring(reason or "auto"), "waiting for WiFi")
            return
        end
        self:_submitSyncJob(
            "annotations",
            _("Highlight sync"),
            reason or "auto",
            BridgeSyncCoordinator.PRIORITY.automatic,
            true,
            function()
                local ok = self:syncAnnotations(silent == nil and true or silent)
                if not ok then
                    self:logWarn("Highlight sync after", tostring(reason or "auto"), "did not complete")
                end
                return ok
            end
        )
    end)
    return true
end

function BridgeSync:_scheduleAutoStatsSync(reason, delay_seconds, silent, retries_left)
    if not self.is_enabled or not self.session_tracking_enabled or not self.auto_sync_stats then
        return false
    end
    if self.stats_sync_scheduled then
        self:logInfo("Reading stats sync already scheduled; skipping duplicate", tostring(reason or "auto"))
        return true
    end
    if (os.time() - (self.last_stats_sync_time or 0)) < 300 then
        return false
    end

    self.stats_sync_scheduled = true
    self:_scheduleTask("stats_sync", delay_seconds or 1, function()
        self.stats_sync_scheduled = false
        if not self.is_enabled or not self.session_tracking_enabled or not self.auto_sync_stats then
            return
        end
        if not NetworkMgr:isConnected() then
            self:logInfo("Reading stats sync after", tostring(reason or "auto"), "waiting for WiFi")
            return
        end
        if (os.time() - (self.last_stats_sync_time or 0)) < 300 then
            return
        end
        if reason then
            self:logInfo("Auto-syncing reading stats after", reason)
        end
        self:_submitSyncJob(
            "statistics",
            _("Reading stats sync"),
            reason or "auto",
            BridgeSyncCoordinator.PRIORITY.automatic,
            true,
            function()
            self.stats_sync_in_flight = true
            local ok = self:syncReadingStats(silent == nil and true or silent)
            self.stats_sync_in_flight = false
            -- Only start the 5-minute cooldown once a sync actually succeeds, so a failed attempt
            -- right after wake doesn't block the next reconnect from retrying.
            if ok then
                self.last_stats_sync_time = os.time()
            end
            if not ok then self:logWarn("Reading stats sync after", tostring(reason or "auto"), "did not complete") end
            -- Highlights ride the same cadence: exchange after each stats round.
            self:_scheduleAutoAnnotationSync("stats", 1, true)
            return ok
            end
        )
    end)
    return true
end

function BridgeSync:_maybeAutoSyncStats(reason)
    if not self.is_enabled or not self.session_tracking_enabled or not self.auto_sync_stats then
        return false
    end
    if not NetworkMgr:isConnected() then
        return false
    end
    if (os.time() - (self.last_stats_sync_time or 0)) < 300 then
        return false
    end
    return self:_scheduleAutoStatsSync(reason, 1, true)
end

function BridgeSync:syncAnnotations(silent)
    if silent == nil then silent = false end
    if not self.is_enabled or not self.annotation_sync_enabled then
        if not silent then
            self:_showMessage(_("Highlight sync is disabled"), 3)
        end
        return false
    end
    if not self.server_url or self.server_url == "" or
       not self.username or self.username == "" or
       not self.key or self.key == "" then
        if not silent then
            self:_showMessage(_("Bridge Sync is not configured"), 3)
        end
        return false
    end
    if self.annotation_sync_in_flight then
        if silent then
            self:logInfo("Highlight sync skipped because another highlight sync is already running")
        else
            self:_showMessage(_("Highlight sync is already running"), 2)
        end
        return false
    end
    self.annotation_sync_in_flight = true

    local ok, result, err = pcall(BridgeAnnotations.run, self)
    self.annotation_sync_in_flight = false

    if not ok then
        self:logErr("Highlight sync crashed:", tostring(result))
        if not silent then
            self:_showMessage(T(_("Highlight sync failed: %1"), tostring(result)), 5)
        end
        return false
    end
    if result == nil then
        self:logWarn("Highlight sync failed:", tostring(err))
        if not silent then
            self:_showMessage(T(_("Highlight sync failed: %1"), tostring(err or "Unknown error")), 5)
        end
        return false
    end

    self:logInfo("Highlight sync:", tostring(result.books), "book(s),",
        tostring(result.uploaded), "uploaded,", tostring(result.applied), "applied,",
        tostring(result.deleted), "deleted")
    if not silent then
        if result.disabled then
            self:_showMessage(_("Highlight sync is disabled on the bridge"), 4)
        elseif (result.books or 0) == 0 then
            self:_showMessage(_("No books with highlights to sync."), 3)
        else
            self:_showMessage(T(
                _("Highlights synced.\nBooks: %1\nUploaded: %2\nApplied from other devices: %3\nDeleted: %4"),
                result.books or 0, result.uploaded or 0, result.applied or 0, result.deleted or 0
            ), 4)
        end
    end
    return true
end

function BridgeSync:onBridgeSyncSyncAnnotations()
    self:_submitSyncJob(
        "annotations", _("Highlight sync"), "manual",
        BridgeSyncCoordinator.PRIORITY.manual, false,
        function() return self:syncAnnotations(false) end
    )
    return true
end

function BridgeSync:_startAnnotationSweepNow(coordinator_done)
    if not self.is_enabled or not self.annotation_sync_enabled then
        self:_showMessage(_("Highlight sync is disabled"), 3)
        if coordinator_done then coordinator_done() end
        return
    end
    if not self.server_url or self.server_url == "" or
       not self.username or self.username == "" or
       not self.key or self.key == "" then
        self:_showMessage(_("Bridge Sync is not configured"), 3)
        if coordinator_done then coordinator_done() end
        return
    end
    if BridgeSweep.isRunning() then
        self:_showMessage(_("Highlight sweep is already running"), 3)
        if coordinator_done then coordinator_done() end
        return
    end
    if not NetworkMgr:isConnected() then
        self:_showMessage(_("No network connection"), 3)
        if coordinator_done then coordinator_done() end
        return
    end

    local started, err = BridgeSweep.start(
        self,
        function(index, total)
            if index % 25 == 0 then
                self:logInfo("Highlight sweep progress:", tostring(index), "of", tostring(total))
            end
        end,
        function(totals, message)
            local summary = T(
                _("Highlight sweep %1.\nBooks: %2 (skipped %3)\nUploaded: %4\nApplied: %5\nDeleted: %6"),
                message and _("stopped") or _("finished"),
                totals.books or 0, totals.skipped or 0,
                totals.uploaded or 0, totals.applied or 0, totals.deleted or 0
            )
            if message then
                summary = summary .. "\n" .. tostring(message)
            end
            self:logInfo("Highlight sweep done:", tostring(message or "completed"))
            self:_showMessage(summary, 8)
            if coordinator_done then coordinator_done() end
        end
    )
    if started then
        self:_showMessage(_("Highlight sweep started in the background."), 3)
    else
        self:_showMessage(T(_("Highlight sweep not started: %1"), tostring(err)), 4)
        if coordinator_done then coordinator_done() end
    end
end

function BridgeSync:startAnnotationSweep()
    local result = self.sync_coordinator:submit({
        owner = self,
        family = "annotation_sweep",
        label = _("Highlight sweep"),
        source = "manual",
        priority = BridgeSyncCoordinator.PRIORITY.manual,
        run = function(done)
            self:_startAnnotationSweepNow(done)
        end,
        on_error = function(err)
            self:logErr("Highlight sweep could not start:", tostring(err))
        end,
    })
    if result ~= "started" then self:_showMessage(_("Highlight sweep queued"), 2) end
end

function BridgeSync:onBridgeSyncSweepAnnotations()
    self:startAnnotationSweep()
    return true
end

function BridgeSync:onResume()
    if not self.is_enabled then
        return false
    end

    -- Restart session tracking if a book is open
    if self.session_tracking_enabled and self.ui and self.ui.document then
        self:startSession()
    end

    -- A fresh wake's network route isn't usable for ~20-30s, so don't fire uploads now.
    -- Funnel everything (pending sessions + stats + manifest) into one WiFi-polling job;
    -- it runs sessions/stats regardless of the book-sync toggles, and the manifest portion
    -- still respects manual_only / auto_sync_on_resume / cooldown / reading.
    if self.auto_sync_on_resume and not self:_isCooldownActive() then
        self.needs_wake_sync = true
    end
    if self:_hasWakeWork() then
        self:_scheduleSync(self.wake_sync_delay_seconds, true)
    end
    return false
end

function BridgeSync:onNetworkConnected()
    if not self.is_enabled then
        return false
    end

    if self.auto_sync_on_network and not self:_isCooldownActive() then
        self.needs_wake_sync = true
    end
    -- Run the one connectivity-gated job after a short settle delay. The sync_scheduled guard
    -- collapses a resume+connect pair into a single job, so onResume's wake delay still wins.
    if self:_hasWakeWork() then
        self:_scheduleSync(10, true)
    end
    self:_scheduleTask("plugin_update", 2, function()
        self:_maybeCheckForPluginUpdate()
    end)
    return false
end

function BridgeSync:_fileExists(path)
    return lfs.attributes(path, "mode") == "file"
end

function BridgeSync:_calculateBookHash(file_path)
    local file = io.open(file_path, "rb")
    if not file then
        return nil
    end

    local base = 1024
    local block_size = 1024
    local buffer = {}
    local file_size = file:seek("end")
    file:seek("set", 0)

    for i = -1, 10 do
        local position = (i == -1) and 0 or bit.lshift(base, 2 * i)
        if position >= file_size then
            break
        end
        file:seek("set", position)
        local chunk = file:read(block_size)
        if chunk then
            table.insert(buffer, chunk)
        end
    end

    file:close()
    return md5(table.concat(buffer))
end

function BridgeSync:_buildHashIndex()
    local index = {}
    if lfs.attributes(self.download_dir, "mode") ~= "directory" then
        return index
    end

    local hash_cache = self:_getStateScalarJSON("hash_cache", {})
    local new_cache = {}

    for entry in lfs.dir(self.download_dir) do
        if entry ~= "." and entry ~= ".." and not entry:match("%.part$") then
            local path = self.download_dir .. "/" .. entry
            local attrs = lfs.attributes(path)
            if attrs and attrs.mode == "file" then
                local cache_key = path .. ":" .. tostring(attrs.modification) .. ":" .. tostring(attrs.size)
                local hash = hash_cache[cache_key]
                if not hash then
                    hash = self:_calculateBookHash(path)
                end
                if hash then
                    new_cache[cache_key] = hash
                    if not index[hash] then
                        index[hash] = path
                    end
                end
            end
        end
    end

    self:_saveStateScalarJSON("hash_cache", new_cache)
    return index
end

function BridgeSync:_normalizeManagedPath(path)
    if not path then
        return nil
    end
    local normalized = tostring(path):gsub("\\", "/"):gsub("/+", "/")
    if #normalized > 1 then
        normalized = normalized:gsub("/$", "")
    end
    local lower = normalized:lower()
    if lower:match("^/mnt/onboard/") or lower:match("^/mnt/us/") then
        return lower
    end
    return normalized
end

function BridgeSync:_managedPathsEqual(left, right)
    local normalized_left = self:_normalizeManagedPath(left)
    local normalized_right = self:_normalizeManagedPath(right)
    return normalized_left ~= nil and normalized_left == normalized_right
end

function BridgeSync:_findTrackedAbsIdByPath(items, path)
    for abs_id, entry in pairs(items) do
        if self:_managedPathsEqual(entry.local_path, path) then
            return abs_id
        end
    end
    return nil
end

function BridgeSync:_safeRemove(path)
    if not path or not self:_fileExists(path) then
        return true
    end
    local ok, err = os.remove(path)
    return ok == true, err
end

function BridgeSync:_removeTree(path)
    local mode = lfs.attributes(path, "mode")
    if mode == "file" then
        local ok, err = os.remove(path)
        return ok == true, err
    end
    if mode ~= "directory" then
        return true
    end

    for entry in lfs.dir(path) do
        if entry ~= "." and entry ~= ".." then
            local child = path .. "/" .. entry
            local child_ok, child_err = self:_removeTree(child)
            if not child_ok then
                return false, child_err
            end
        end
    end
    local ok, err = lfs.rmdir(path)
    return ok == true, err
end

function BridgeSync:_deleteManagedFile(path)
    if not path or path == "" then
        return true
    end
    local file_ok, file_err = self:_safeRemove(path)
    local sidecar_ok, sidecar_err = self:_removeTree(path .. ".sdr")
    if not file_ok or not sidecar_ok then
        return false, file_err or sidecar_err or "managed file deletion failed"
    end
    return true
end

function BridgeSync:_moveFile(source_path, target_path)
    if self:_managedPathsEqual(source_path, target_path) then
        return true
    end

    self:_safeRemove(target_path)
    local ok, err = os.rename(source_path, target_path)
    if not ok then
        return false, err or "rename failed"
    end

    local old_sidecar = source_path .. ".sdr"
    local new_sidecar = target_path .. ".sdr"
    if lfs.attributes(old_sidecar, "mode") == "directory" then
        self:_removeTree(new_sidecar)
        os.rename(old_sidecar, new_sidecar)
    end
    return true
end

function BridgeSync:_currentDocumentPath()
    local doc = self.ui and self.ui.document
    if not doc then
        return nil
    end
    return doc.file
end

function BridgeSync:_isCurrentDocument(path)
    local current = self:_currentDocumentPath()
    return self:_managedPathsEqual(current, path)
end

function BridgeSync:_runSync()
    local dir_ok, dir_err = self:_ensureDirectory(self.download_dir)
    if not dir_ok then
        -- level 0: no "main.lua:NNN:" prefix, so the user sees the real reason.
        error(T(
            _("Cannot create the Managed Folder \"%1\" (%2). Pick a writable folder under Bridge Sync > Managed Folder."),
            tostring(self.download_dir or ""),
            tostring(dir_err or "unknown error")
        ), 0)
    end

    local local_revision = tostring(self:_getStateScalar("revision") or "")
    local ok, manifest_or_error = self.api:getManifest()
    if not ok then
        error(manifest_or_error or "Failed to fetch manifest")
    end

    local manifest = manifest_or_error
    local remote_revision = tostring(manifest.revision or "")
    if remote_revision ~= "" and local_revision ~= "" and remote_revision == local_revision then
        self:logInfo("Manifest revision unchanged, skipping file checks")
        return {
            downloaded = 0,
            skipped = 0,
            renamed = 0,
            deleted = 0,
            deferred = 0,
            errors = 0,
            remaining = 0,
            revision = remote_revision,
            unchanged = true,
        }
    end

    local remote_books = manifest.books or {}
    local remote_by_abs = {}
    local remote_paths = {}
    local items = self:_loadStateItems()
    local hash_index = nil
    local function getHashIndex()
        if not hash_index then
            hash_index = self:_buildHashIndex()
        end
        return hash_index
    end
    local downloaded, skipped, renamed, deleted, deferred, errors = 0, 0, 0, 0, 0, 0
    local remaining, download_attempts = 0, 0

    for _, book in ipairs(remote_books) do
        remote_by_abs[book.abs_id] = true
        local target_path = self.download_dir .. "/" .. book.filename
        remote_paths[self:_normalizeManagedPath(target_path)] = true
        local entry = items[book.abs_id]
        local previous_entry = entry and {
            local_path = entry.local_path,
            filename = entry.filename,
            content_hash = entry.content_hash,
            pending_delete = entry.pending_delete,
        } or nil
        local reused_path = nil

        if entry and entry.local_path and self:_fileExists(entry.local_path) and entry.content_hash == book.content_hash then
            reused_path = entry.local_path
        elseif self:_fileExists(target_path) then
            local existing_hash = self:_calculateBookHash(target_path)
            if existing_hash == book.content_hash then
                reused_path = target_path
            end
        end

        if not reused_path then
            local indexed_path = getHashIndex()[book.content_hash]
            if indexed_path and self:_fileExists(indexed_path) then
                local tracked_abs_id = self:_findTrackedAbsIdByPath(items, indexed_path)
                if not tracked_abs_id or tracked_abs_id == book.abs_id then
                    reused_path = indexed_path
                end
            end
        end

        if reused_path then
            if not self:_managedPathsEqual(reused_path, target_path) then
                local move_ok = self:_moveFile(reused_path, target_path)
                if move_ok then
                    renamed = renamed + 1
                else
                    errors = errors + 1
                end
            else
                skipped = skipped + 1
            end

            if self:_fileExists(target_path) then
                items[book.abs_id] = {
                    local_path = target_path,
                    filename = book.filename,
                    content_hash = book.content_hash,
                    shelves = book.shelves,
                }
                if hash_index then hash_index[book.content_hash] = target_path end
            end
        else
            if not ManifestRules.downloadAllowed(download_attempts, self.max_downloads_per_sync) then
                remaining = remaining + 1
            else
                download_attempts = download_attempts + 1
                local temp_path = target_path .. ".part"
                self:_safeRemove(temp_path)
                local dl_ok, dl_err = self.api:downloadBook(book.download_path, temp_path, book.size)
                if not dl_ok then
                    self:logWarn("Download failed for", book.abs_id, dl_err or "")
                    errors = errors + 1
                    self:_safeRemove(temp_path)
                else
                    local downloaded_size = tonumber((lfs.attributes(temp_path, "size"))) or 0
                    local expected_size = tonumber(book.size) or 0
                    local downloaded_hash = downloaded_size > 0 and self:_calculateBookHash(temp_path) or nil
                    if downloaded_size <= 0 or (expected_size > 0 and downloaded_size ~= expected_size)
                        or not downloaded_hash or downloaded_hash ~= book.content_hash
                    then
                        self:logWarn("Hash mismatch for", book.abs_id, downloaded_hash or "unavailable", book.content_hash)
                        errors = errors + 1
                        self:_safeRemove(temp_path)
                    else
                        local move_ok, move_err = os.rename(temp_path, target_path)
                        if not move_ok and self:_fileExists(target_path) then
                            -- Same atomic-install idiom as _installPluginZip, sized down
                            -- to a single file: os.rename is C rename(), which fails with
                            -- EEXIST on Windows when the destination already exists
                            -- (POSIX replaces it atomically instead). Verification has
                            -- already passed, so move the previous good book aside
                            -- rather than deleting it, retry the publish once, and
                            -- restore the backup if that retry also fails - a broken
                            -- publication must never cost the reader the copy they
                            -- already had. The backup is a sibling of the target so both
                            -- renames stay on one filesystem.
                            local backup_path = target_path .. ".bak"
                            self:_safeRemove(backup_path)
                            local moved_aside = os.rename(target_path, backup_path)
                            if moved_aside then
                                move_ok, move_err = os.rename(temp_path, target_path)
                                if move_ok then
                                    self:_safeRemove(backup_path)
                                else
                                    os.rename(backup_path, target_path)
                                end
                            end
                        end
                        if not move_ok then
                            self:logWarn("Rename failed for", book.abs_id, move_err or "")
                            errors = errors + 1
                            self:_safeRemove(temp_path)
                        else
                            downloaded = downloaded + 1
                            if previous_entry
                                and self:_managedPathsEqual(previous_entry.local_path, target_path)
                                and previous_entry.content_hash
                                and previous_entry.content_hash ~= book.content_hash
                            then
                                self:_removeTree(target_path .. ".sdr")
                            end
                            items[book.abs_id] = {
                                local_path = target_path,
                                filename = book.filename,
                                content_hash = book.content_hash,
                                shelves = book.shelves,
                            }
                            if hash_index then hash_index[book.content_hash] = target_path end
                        end
                    end
                end
            end
        end
    end

    if self.delete_removed_books then
        for abs_id, entry in pairs(items) do
            if not remote_by_abs[abs_id] then
                if remote_paths[self:_normalizeManagedPath(entry.local_path)] then
                    -- A replacement ID can own the same file as a retired match.
                    items[abs_id] = nil
                elseif self:_isCurrentDocument(entry.local_path) then
                    entry.pending_delete = true
                    items[abs_id] = entry
                    deferred = deferred + 1
                else
                    local delete_ok, delete_err = self:_deleteManagedFile(entry.local_path)
                    if delete_ok then
                        items[abs_id] = nil
                        deleted = deleted + 1
                    else
                        errors = errors + 1
                        self:logWarn("Managed file deletion failed for", abs_id,
                            entry.local_path or "", delete_err or "")
                    end
                end
            elseif entry.pending_delete then
                entry.pending_delete = nil
                items[abs_id] = entry
            end
        end
    end

    if remaining > 0 then
        self:logInfo("Download cap reached:", remaining, "book(s) deferred to next sync")
    end

    self:_updateCollections(items)
    -- A dirty sweep (errors or capped downloads) must not persist the server
    -- revision, or the next sync's revision-match short-circuit would never retry.
    self:_saveState(items, ManifestRules.revisionToPersist(manifest.revision, errors, remaining))
    return {
        downloaded = downloaded,
        skipped = skipped,
        renamed = renamed,
        deleted = deleted,
        deferred = deferred,
        errors = errors,
        remaining = remaining,
        revision = remote_revision,
        unchanged = false,
    }
end

function BridgeSync:_updateCollections(items)
    local ok_read, ReadCollection = pcall(require, "readcollection")
    if not ok_read or not ReadCollection then
        self:logWarn("Could not load readcollection for Bridge Sync collections")
        return
    end

    local ok_reload, reload_err = pcall(function()
        if ReadCollection._read then
            ReadCollection:_read()
        end
    end)
    if not ok_reload then
        self:logWarn("Could not reload KOReader collections:", tostring(reload_err or "unknown error"))
        return
    end

    local shelf_books = {}
    for _, entry in pairs(items) do
        if entry.shelves and type(entry.shelves) == "table" and entry.local_path and self:_fileExists(entry.local_path) then
            for _, shelf_name in ipairs(entry.shelves) do
                if not shelf_books[shelf_name] then
                    shelf_books[shelf_name] = {
                        files = {},
                        seen = {},
                    }
                end
                if not shelf_books[shelf_name].seen[entry.local_path] then
                    shelf_books[shelf_name].seen[entry.local_path] = true
                    table.insert(shelf_books[shelf_name].files, entry.local_path)
                end
            end
        end
    end

    local prev_managed = self.settings:readSetting("managed_collections") or {}
    local new_managed = {}
    local ok_update, update_err = pcall(function()
        for _, name in ipairs(prev_managed) do
            if ReadCollection.removeCollection then
                ReadCollection:removeCollection(name)
            end
        end

        for shelf_name, shelf_data in pairs(shelf_books) do
            if ReadCollection.removeCollection then
                ReadCollection:removeCollection(shelf_name)
            end
            ReadCollection:addCollection(shelf_name)
            if ReadCollection.coll_settings and ReadCollection.coll_settings[shelf_name] then
                ReadCollection.coll_settings[shelf_name].order = 1
            end
            for _, file_path in ipairs(shelf_data.files) do
                ReadCollection:addItem(file_path, shelf_name)
            end
            table.insert(new_managed, shelf_name)
        end

        ReadCollection:write()
    end)
    if not ok_update then
        self:logWarn("Could not update KOReader collections:", tostring(update_err or "unknown error"))
        return
    end

    self.settings:saveSetting("managed_collections", new_managed)
    self.settings:flush()
    self:_refreshCollectionsUI()

    if #new_managed > 0 then
        self:logInfo("Updated", #new_managed, "KOReader collection(s) from shelf data")
    end
end

function BridgeSync:syncFromBridge(silent)
    if silent == nil then
        silent = false
    end

    if self.sync_in_progress then
        if silent then
            self:logInfo("Reading stats sync skipped because Bridge Sync is already running")
        end
        if not silent then
            self:_showMessage(_("Bridge Sync is already running"), 2)
        end
        return false
    end

    if not self.server_url or self.server_url == "" or
       not self.username or self.username == "" or
       not self.key or self.key == "" then
        if not silent then
            self:_showMessage(_("Bridge Sync is not configured"), 3)
        end
        return false
    end

    local network_ok, network_err = self:_preflightNetwork(silent)
    if not network_ok then
        self:logWarn(network_err)
        if not silent then
            self:_showMessage(network_err, 4)
        end
        return false
    end

    self.sync_in_progress = true
    local info_msg = nil
    if not silent then
        info_msg = InfoMessage:new{
            text = _("Syncing bridge matches..."),
            timeout = 0,
        }
        UIManager:show(info_msg)
        UIManager:forceRePaint()
    end

    local subprocess_ok, success, result = self:_runInSubprocess(function()
        return pcall(function()
            return self:_runSync()
        end)
    end, { needs_sqlite = true })

    if info_msg then
        UIManager:close(info_msg)
    end
    self.sync_in_progress = false

    if not subprocess_ok then
        self:logErr("Bridge Sync subprocess failed", success or "")
        if not silent then
            self:_showMessage(T(_("Bridge Sync failed: %1"), tostring(success or "Subprocess failed")), 5)
        end
        self:_uploadDeviceLogTail("book_sync", "failure")
        return false
    end

    if not success then
        self:logErr(result or "Unknown sync error")
        if not silent then
            self:_showMessage(T(_("Bridge Sync failed: %1"), tostring(result or "Unknown error")), 5)
        end
        self:_uploadDeviceLogTail("book_sync", "failure")
        return false
    end

    local message
    if result.unchanged then
        message = _("Bridge Sync complete. No changes found.")
    else
        message = T(
            _("Bridge Sync complete.\nDownloaded: %1\nSkipped: %2\nRenamed: %3\nDeleted: %4\nDeferred: %5\nErrors: %6"),
            result.downloaded,
            result.skipped,
            result.renamed,
            result.deleted,
            result.deferred,
            result.errors
        )
        if (result.remaining or 0) > 0 then
            message = message .. "\n" .. T(_("Remaining: %1 (will continue next sync)"), result.remaining)
        end
        if (result.errors or 0) > 0 then
            message = message .. "\n" .. _("Failed downloads will be retried on the next sync.")
        end
    end
    self:logInfo(message)
    if not silent then
        self:_showMessage(message, 5)
    end

    local FileManager = require("apps/filemanager/filemanager")
    local current_filemanager_path = self:_currentFileManagerPath()
    if not silent
        and FileManager.instance
        and current_filemanager_path ~= nil
        and current_filemanager_path == self:_normalizePath(self.download_dir)
    then
        FileManager.instance:reinit(self.download_dir)
    end

    self:_maybeAutoSyncStats("manifest sync")

    local sync_status = ((tonumber(result.errors) or 0) > 0 or (tonumber(result.remaining) or 0) > 0) and "partial" or "success"
    self:_uploadDeviceLogTail("book_sync", sync_status)

    return true
end

function BridgeSync:testConnection()
    if not self.server_url or self.server_url == "" then
        self:_showMessage(_("Server URL is not configured"), 2)
        return
    end
    local network_ok, network_err = self:_preflightNetwork()
    if not network_ok then
        self:logWarn(network_err)
        self:_showMessage(network_err, 4)
        return
    end

    local info_msg = InfoMessage:new{
        text = _("Testing bridge connection..."),
        timeout = 0,
    }
    UIManager:show(info_msg)
    UIManager:forceRePaint()

    local subprocess_ok, ok, message = self:_runInSubprocess(function()
        return self.api:testAuth()
    end, { inline = true })

    UIManager:close(info_msg)

    if not subprocess_ok then
        self:logErr("Bridge connection test subprocess failed", ok or "")
        self:_showMessage(T(_("Bridge connection test failed: %1"), tostring(ok or "Subprocess failed")), 5)
        return
    end

    if ok then
        local display_msg = (message and message ~= "") and message or _("Authentication successful")
        self:_showMessage(display_msg, 2)
    else
        self:logWarn(message or "Authentication failed")
        self:_showMessage(message or _("Authentication failed"), 4)
    end
end

-- ── Reading Session Tracking ──

function BridgeSync:_getCurrentProgress()
    if not self.ui or not self.ui.document then
        return 0, "0"
    end

    local progress = 0
    local location = "0"

    if self.ui.document.info and self.ui.document.info.has_pages then
        local current_page = nil
        if self.view and self.view.state and self.view.state.page then
            current_page = self.view.state.page
        elseif self.ui.paging then
            current_page = self.ui.paging:getCurrentPage()
        end
        local total_pages = self.ui.document:getPageCount()
        if current_page and total_pages and total_pages > 0 then
            progress = (current_page / total_pages) * 100
            location = tostring(current_page)
        end
    elseif self.ui.rolling then
        local cur_page = self.ui.document:getCurrentPage()
        local total_pages = self.ui.document:getPageCount()
        if cur_page and total_pages and total_pages > 0 then
            progress = (cur_page / total_pages) * 100
            location = tostring(cur_page)
        end
    end

    return progress, location
end

function BridgeSync:_getBookType(file_path)
    if not file_path then
        return "EPUB"
    end
    local ext = file_path:match("^.+%.(.+)$")
    if ext then
        ext = ext:upper()
        if ext == "PDF" then
            return "PDF"
        elseif ext == "CBZ" or ext == "CBR" then
            return "CBX"
        end
    end
    return "EPUB"
end

function BridgeSync:_resolveAbsId(file_path)
    local items = self:_loadStateItems()
    for abs_id, entry in pairs(items) do
        if entry.local_path and self:_managedPathsEqual(entry.local_path, file_path) then
            return abs_id
        end
    end
    return nil
end

function BridgeSync:startSession()
    if not self.is_enabled or not self.session_tracking_enabled then
        return
    end
    if not self.ui or not self.ui.document then
        return
    end

    local file_path = self.ui.document.file
    if not file_path then
        return
    end
    file_path = tostring(file_path)

    local abs_id = self:_resolveAbsId(file_path)
    local doc_hash = nil
    if not abs_id then
        doc_hash = self:_calculateBookHash(file_path)
    end
    local start_progress, start_page = self:_getCurrentProgress()

    self.current_session = {
        file_path = file_path,
        abs_id = abs_id,
        document_hash = doc_hash,
        start_time = os.time(),
        start_progress = start_progress,
        start_page = start_page,
        book_type = self:_getBookType(file_path),
    }

    self:logInfo("Session started for", file_path, "abs_id:", abs_id or "nil", "hash:", doc_hash or "n/a", "at", start_progress, "%")
end

function BridgeSync:endSession(options)
    options = options or {}
    local force_queue = options.force_queue or false

    if not self.current_session then
        return
    end

    local end_time = os.time()
    local end_progress, end_page = self:_getCurrentProgress()
    local duration_seconds = end_time - self.current_session.start_time

    local start_loc = tonumber(self.current_session.start_page) or 0
    local end_loc = tonumber(end_page) or 0
    local pages_read = math.abs(end_loc - start_loc)

    if duration_seconds < self.min_session_duration then
        self:logInfo("Session too short:", duration_seconds, "s <", self.min_session_duration, "s")
        self.current_session = nil
        return
    end

    if pages_read <= 0 then
        self:logInfo("No page progress, skipping session")
        self.current_session = nil
        return
    end

    local session = {
        abs_id = self.current_session.abs_id,
        document_hash = self.current_session.document_hash,
        session_type = self.current_session.book_type,
        start_time = self.current_session.start_time,
        end_time = end_time,
        duration_seconds = duration_seconds,
        start_progress = self.current_session.start_progress,
        end_progress = end_progress,
        start_page = start_loc,
        end_page = end_loc,
    }

    -- Session collapsing: merge into an existing un-uploaded session for the
    -- same book within the merge threshold, or store as a new session.
    local merged = false
    if self.sqlite_available then
        local existing = nil
        if session.abs_id then
            existing = self.sqlite_state:find_mergeable_pending_session(
                session.abs_id, session.start_time, self.session_merge_threshold or 300)
        end
        if existing and self.sqlite_state:merge_pending_session_end(
                existing.session_id, session.end_time,
                session.end_page, session.end_progress, session.duration_seconds) then
            merged = true
            -- Mirror the merge onto the in-memory entry
            for _, s in ipairs(self.pending_sessions) do
                if s.session_id == existing.session_id then
                    BridgeSessions.applyMerge(s, session)
                    break
                end
            end
            self:logInfo("Session merged into existing", existing.session_id,
                "for", session.abs_id or session.document_hash or "unknown")
        else
            session.session_id = self.sqlite_state:add_pending_session(session)
            table.insert(self.pending_sessions, session)
        end
    else
        -- LuaSettings fallback: in-memory merge, then persist the list
        merged = self:_mergeOrAppendSession(session)
    end

    self:logInfo("Session ended:", duration_seconds, "s,", pages_read, "pages,",
        self.current_session.start_progress, "% ->", end_progress, "%", merged and "(merged)" or "")

    self.current_session = nil

    if not force_queue and NetworkMgr:isConnected() then
        self:_maybeUploadPendingSessions("close")
    end
end

-- LuaSettings fallback: merge `session` into the in-memory pending list when
-- possible (see bridge_sessions.lua), append otherwise, then persist the
-- list. Returns true if merged, false if appended.
function BridgeSync:_mergeOrAppendSession(session)
    local merged = BridgeSessions.mergeIntoPending(
        self.pending_sessions, session, self.session_merge_threshold or 300)
    if not merged then
        table.insert(self.pending_sessions, session)
    end
    self:_savePendingSessions(self.pending_sessions)
    return merged
end

-- The bridge reports why it turned a session down. A reason the device can never
-- satisfy must not sit in the queue re-uploading on every wake.
function BridgeSync:_shouldAbandonSession(session, result)
    local reason = result and result.reason or nil
    if reason == "invalid_session" then
        self:logWarn("Abandoning malformed session", tostring(session.session_id))
        return true
    end
    if reason ~= "book_not_found" then
        return false
    end
    local session_id = tostring(session.session_id or "")
    if session_id == "" then
        return true
    end
    local attempts = (tonumber(self.session_upload_attempts[session_id]) or 0) + 1
    self.session_upload_attempts[session_id] = attempts
    if attempts >= SESSION_MAX_BOOK_NOT_FOUND_ATTEMPTS then
        self:logWarn("Abandoning session for a book the bridge does not have:",
            tostring(session.abs_id or session.document_hash or session_id),
            "after", tostring(attempts), "attempts")
        return true
    end
    return false
end

-- Forget attempt counts for sessions that are no longer queued, so the map
-- cannot grow without bound as sessions are accepted or abandoned.
function BridgeSync:_pruneSessionUploadAttempts()
    local live = {}
    for _, session in ipairs(self.pending_sessions) do
        if session.session_id then
            live[tostring(session.session_id)] = true
        end
    end
    local kept = {}
    for session_id, attempts in pairs(self.session_upload_attempts) do
        if live[session_id] then
            kept[session_id] = attempts
        end
    end
    self.session_upload_attempts = kept
    self:_saveStateScalarJSON("session_upload_attempts", kept)
end

function BridgeSync:_uploadSessions()
    if #self.pending_sessions == 0 then
        return true
    end

    self:logInfo("Uploading", #self.pending_sessions, "pending sessions")

    local submitted_sessions = self.pending_sessions
    local ok, code, body = self.api:uploadSessions(submitted_sessions)
    if ok then
        local response = nil
        local parsed, decoded = pcall(json.decode, body or "{}")
        if parsed and type(decoded) == "table" then
            response = decoded
        end

        local accepted_sessions = submitted_sessions
        local retained_sessions = {}
        local abandoned_sessions = {}
        local has_per_session_results = response and type(response.results) == "table"
        if has_per_session_results then
            accepted_sessions = {}
            local results_by_index = {}
            for _, result in ipairs(response.results) do
                if type(result) == "table" and tonumber(result.index) then
                    results_by_index[tonumber(result.index)] = result
                end
            end
            for index, session in ipairs(submitted_sessions) do
                local result = results_by_index[index]
                if result and result.accepted == true then
                    table.insert(accepted_sessions, session)
                elseif self:_shouldAbandonSession(session, result) then
                    table.insert(abandoned_sessions, session)
                else
                    table.insert(retained_sessions, session)
                end
            end
        elseif response and tonumber(response.rejected or 0) > 0 then
            accepted_sessions = {}
            retained_sessions = submitted_sessions
        end

        -- Mark SQLite rows as uploaded so they aren't reloaded on restart.
        -- Abandoned rows are marked too: the bridge will never take them, so the
        -- row has to stop coming back rather than be retried on every wake.
        if self.sqlite_available then
            local uploaded_ids = {}
            for _, s in ipairs(accepted_sessions) do
                if s.session_id then
                    table.insert(uploaded_ids, s.session_id)
                end
            end
            for _, s in ipairs(abandoned_sessions) do
                if s.session_id then
                    table.insert(uploaded_ids, s.session_id)
                end
            end
            if #uploaded_ids > 0 then
                local marked = self.sqlite_state:mark_sessions_uploaded(uploaded_ids)
                if not marked then
                    self:logWarn(
                        "Session upload reached the bridge but local SQLite acknowledgement failed; retaining queue for retry"
                    )
                    self:_uploadDeviceLogTail("session_upload", "failure")
                    return false
                end
            end
        end

        self.pending_sessions = retained_sessions
        self:_savePendingSessions(self.pending_sessions)
        self:_pruneSessionUploadAttempts()
        if #abandoned_sessions > 0 then
            self:logWarn("Abandoned", #abandoned_sessions,
                "session(s) the bridge will never accept")
        end
        if #retained_sessions > 0 then
            self:logWarn("Session upload partially accepted:", #accepted_sessions,
                "accepted,", #retained_sessions, "retained for retry")
            self:_uploadDeviceLogTail("session_upload", "partial")
            return false
        end

        self:logInfo("Sessions uploaded successfully")
        self:_uploadDeviceLogTail("session_upload", "success")
        return true
    else
        self:logWarn("Session upload failed:", code or "", body or "", "- will retry later")
        self:_uploadDeviceLogTail("session_upload", "failure")
        return false
    end
end

function BridgeSync:_findStatisticsDbPath()
    local settings_dir = DataStorage:getSettingsDir()
    local candidates = {
        settings_dir .. "/statistics.sqlite",
        settings_dir .. "/statistics.sqlite3",
    }
    for _, path in ipairs(candidates) do
        if self:_fileExists(path) then
            return path
        end
    end
    return nil
end

function BridgeSync:_flushStatisticsDatabase()
    local ok_reader, ReaderUI = pcall(require, "apps/reader/readerui")
    if not ok_reader or not ReaderUI or not ReaderUI.instance then
        return
    end

    local ui = ReaderUI.instance
    if ui and ui.statistics and ui.statistics.is_doc then
        local ok_flush, err = pcall(function()
            ui.statistics:insertDB()
        end)
        if ok_flush then
            self:logInfo("Flushed KOReader statistics DB before sync")
        else
            self:logWarn("Failed to flush KOReader statistics DB:", tostring(err or "unknown error"))
        end
    end
end

function BridgeSync:_currentDeviceIdentity()
    local device = "KOReader"
    local device_id = ""

    local ok_device, Device = pcall(require, "device")
    if ok_device and Device then
        device = tostring(Device.friendly_name or Device.model or device)
    end

    if G_reader_settings and G_reader_settings.readSetting then
        device_id = tostring(G_reader_settings:readSetting("device_id") or "")
    end

    return device, device_id
end

-- Read every piece of persisted state the statistics sync needs, in the
-- parent process, so the forked sync child never touches the state backend.
function BridgeSync:_snapshotStatsSyncState()
    return {
        last_uploaded_device_key = tostring(self:_getStateScalar("stats_last_uploaded_device_key") or ""),
        last_uploaded_start_time = tonumber(self:_getStateScalar("stats_last_uploaded_start_time")) or 0,
        last_merged_watermark = tonumber(self:_getStateScalar("stats_last_merged_watermark")) or 0,
        merge_backfill_done = self:_getStateScalar("stats_merge_backfill_done") and true or false,
        stats_fingerprint = self:_getSyncFingerprint("bridge", "statistics", "*"),
    }
end

function BridgeSync:_collectStatisticsPayload(stats_state)
    if not SQ3 then
        return nil, _("SQLite support is unavailable in this KOReader build")
    end

    self:_flushStatisticsDatabase()

    local db_path = self:_findStatisticsDbPath()
    if not db_path then
        return nil, _("statistics.sqlite was not found in the KOReader settings folder")
    end

    local device, device_id = self:_currentDeviceIdentity()
    local device_key = device_id ~= "" and device_id or device
    local last_uploaded_device_key = stats_state.last_uploaded_device_key
    local last_uploaded_start_time = stats_state.last_uploaded_start_time
    if last_uploaded_device_key ~= "" and last_uploaded_device_key ~= device_key then
        last_uploaded_start_time = 0
    end
    local replay_from = math.max(0, math.floor(last_uploaded_start_time - 300))

    -- Differential sync: skip the collection query when the DB file hasn't
    -- been modified since our last successful sync. The fingerprint combines
    -- device identity and file mtime; either changing triggers a full
    -- re-collection. It is captured BEFORE collection so stats written while
    -- a sync is in flight always dirty the next cycle.
    local db_mtime = lfs.attributes(db_path, "modification")
    local db_fingerprint = tostring(device_key) .. ":" .. tostring(db_mtime or "")
    if stats_state.stats_fingerprint ~= nil and stats_state.stats_fingerprint == db_fingerprint then
        self:logInfo("Statistics DB unchanged since last sync, skipping payload collection")
        return {
            device = device,
            device_id = device_id,
            device_key = device_key,
            replay_from = replay_from,
            watermark = last_uploaded_start_time,
            books = {},
            page_stats = {},
            db_fingerprint = db_fingerprint,
        }
    end

    local conn = SQ3.open(db_path)
    if not conn then
        return nil, _("Failed to open KOReader statistics database")
    end

    local ok_payload, payload_or_err = pcall(function()
        local book_result, book_rows = conn:exec("SELECT * FROM book")
        local books_by_id = {}
        for i = 1, book_rows do
            local ko_book_id = tonumber(book_result[1][i])
            local book_md5 = tostring(book_result[10][i] or "")
            if ko_book_id and book_md5 ~= "" then
                books_by_id[ko_book_id] = {
                    ko_book_id = ko_book_id,
                    md5 = book_md5,
                    title = tostring(book_result[2][i] or ""),
                    authors = tostring(book_result[3][i] or ""),
                    pages = tonumber(book_result[7][i]) or 0,
                    total_read_time = tonumber(book_result[11][i]) or 0,
                    total_read_pages = tonumber(book_result[12][i]) or 0,
                }
            end
        end

        local query = string.format(
            "SELECT * FROM page_stat_data WHERE start_time >= %d ORDER BY start_time ASC",
            replay_from
        )
        local page_result, page_rows = conn:exec(query)
        local page_stats = {}
        local included_md5 = {}
        local max_start_time = last_uploaded_start_time

        for i = 1, page_rows do
            local ko_book_id = tonumber(page_result[1][i])
            local book = books_by_id[ko_book_id]
            if book and book.md5 ~= "" then
                local page = tonumber(page_result[2][i]) or 0
                local start_time = tonumber(page_result[3][i]) or 0
                local duration = tonumber(page_result[4][i]) or 0
                local total_pages = tonumber(page_result[5][i]) or 0

                table.insert(page_stats, {
                    md5 = book.md5,
                    page = page,
                    start_time = start_time,
                    duration = duration,
                    total_pages = total_pages,
                })
                included_md5[book.md5] = true
                if start_time > max_start_time then
                    max_start_time = start_time
                end
            end
        end

        local books = {}
        for _, book in pairs(books_by_id) do
            if included_md5[book.md5] then
                table.insert(books, {
                    md5 = book.md5,
                    ko_book_id = book.ko_book_id,
                    title = book.title,
                    authors = book.authors,
                    pages = book.pages,
                    total_read_time = book.total_read_time,
                    total_read_pages = book.total_read_pages,
                })
            end
        end
        table.sort(books, function(a, b)
            return tostring(a.title or "") < tostring(b.title or "")
        end)

        return {
            device = device,
            device_id = device_id,
            device_key = device_key,
            replay_from = replay_from,
            watermark = max_start_time,
            books = books,
            page_stats = page_stats,
            db_fingerprint = db_fingerprint,
        }
    end)

    pcall(function()
        conn:close()
    end)

    if not ok_payload then
        return nil, tostring(payload_or_err or "Failed to read KOReader statistics database")
    end

    return payload_or_err, nil
end

function BridgeSync:_mergeForeignStatistics(payload, stats_state)
    local result = { merged = 0, fetched = 0, watermark = nil, err = nil }

    if not SQ3 then
        result.err = _("SQLite support is unavailable in this KOReader build")
        return result
    end

    local db_path = self:_findStatisticsDbPath()
    if not db_path then
        result.err = _("statistics.sqlite was not found in the KOReader settings folder")
        return result
    end

    local since = stats_state.last_merged_watermark
    if not stats_state.merge_backfill_done then
        -- One-time full re-pull: books never opened here were silently skipped by older
        -- plugin versions, which still advanced the watermark past their events. Fetch
        -- everything once to backfill them; INSERT OR IGNORE keeps the re-merge idempotent.
        since = 0
        result.backfill_pending = true
    end
    local ok, response = self.api:getMergedStatistics(
        payload.device,
        payload.device_id,
        since > 1 and (since - 1) or 0
    )
    if not ok then
        result.err = tostring(response or "Failed to fetch merged statistics")
        return result
    end

    if response.enabled == false then
        return result
    end

    local page_stats = response.page_stats
    if type(page_stats) ~= "table" or #page_stats == 0 then
        result.watermark = tonumber(response.watermark)
        return result
    end
    result.fetched = #page_stats

    -- Book metadata for the foreign md5s, so books never opened on this device can
    -- have their local `book` row created before merging the page events.
    local books_meta_by_md5 = {}
    if type(response.books) == "table" then
        for _, b in ipairs(response.books) do
            local bmd5 = tostring(b.md5 or "")
            if bmd5 ~= "" then
                books_meta_by_md5[bmd5] = {
                    title = tostring(b.title or ""),
                    authors = tostring(b.authors or ""),
                    pages = tonumber(b.pages) or 0,
                }
            end
        end
    end

    local conn = SQ3.open(db_path)
    if not conn then
        result.err = _("Failed to open KOReader statistics database")
        return result
    end

    local ok_merge, merge_err = pcall(function()
        conn:exec("PRAGMA busy_timeout = 5000")

        local books_by_md5 = {}
        local book_result, book_rows = conn:exec("SELECT id, md5 FROM book")
        if book_result and book_rows then
            for i = 1, book_rows do
                local id_book = tonumber(book_result[1][i])
                local book_md5 = tostring(book_result[2][i] or "")
                if id_book and book_md5 ~= "" then
                    books_by_md5[book_md5] = id_book
                end
            end
        end

        conn:exec("BEGIN IMMEDIATE")
        local changes_before = tonumber(conn:exec("SELECT total_changes()")[1][1]) or 0

        local insert_stmt = conn:prepare(
            "INSERT OR IGNORE INTO page_stat_data (id_book, page, start_time, duration, total_pages) VALUES (?, ?, ?, ?, ?)"
        )
        -- KOReader resolves a book by (title, authors, md5), so creating the row with the
        -- foreign device's exact values lets KOReader reuse it (not duplicate) if this
        -- device later opens the file. `pages` seeds KOReader's page rescaling until then.
        local book_insert_stmt = conn:prepare(
            "INSERT OR IGNORE INTO book (title, authors, md5, pages, notes, highlights, last_open, total_read_time, total_read_pages) VALUES (?, ?, ?, ?, 0, 0, ?, 0, 0)"
        )
        local touched = {}
        local created_books = 0
        for _idx, event in ipairs(page_stats) do
            local md5_key = tostring(event.md5 or "")
            local id_book = books_by_md5[md5_key]
            local page = tonumber(event.page)
            local start_time = tonumber(event.start_time)
            local duration = tonumber(event.duration)
            local total_pages = tonumber(event.total_pages)

            -- Book never opened on this device: create its row from the merged metadata.
            if not id_book and md5_key ~= "" and books_meta_by_md5[md5_key] then
                local meta = books_meta_by_md5[md5_key]
                local book_pages = (meta.pages > 0 and meta.pages)
                    or (total_pages and total_pages > 0 and total_pages)
                    or 0
                local last_open = math.floor(start_time or os.time())
                book_insert_stmt:reset()
                book_insert_stmt:bind(meta.title, meta.authors, md5_key, book_pages, last_open)
                book_insert_stmt:step()
                local new_id = tonumber(conn:exec("SELECT last_insert_rowid()")[1][1])
                if new_id and new_id > 0 then
                    id_book = new_id
                    books_by_md5[md5_key] = new_id
                    created_books = created_books + 1
                end
            end

            if id_book and page and start_time and duration and total_pages and total_pages > 0 then
                insert_stmt:reset()
                insert_stmt:bind(id_book, page, start_time, duration, total_pages)
                insert_stmt:step()
                touched[id_book] = true
            end
        end
        insert_stmt:close()
        book_insert_stmt:close()

        local changes_after = tonumber(conn:exec("SELECT total_changes()")[1][1]) or 0
        -- Subtract the book-row inserts so result.merged counts merged page-stat rows only.
        result.merged = math.max(changes_after - changes_before - created_books, 0)

        if result.merged > 0 then
            local update_stmt = conn:prepare([[
                UPDATE book SET
                    total_read_time = (SELECT coalesce(sum(duration), 0) FROM page_stat WHERE id_book = book.id),
                    total_read_pages = (SELECT count(DISTINCT page) FROM page_stat WHERE id_book = book.id)
                WHERE id = ?
            ]])
            for id_book in pairs(touched) do
                update_stmt:reset()
                update_stmt:bind(id_book)
                update_stmt:step()
            end
            update_stmt:close()
        end
        conn:exec("COMMIT")
    end)

    if not ok_merge then
        pcall(function() conn:exec("ROLLBACK") end)
    end
    pcall(function() conn:close() end)

    if not ok_merge then
        result.err = tostring(merge_err or "Failed to merge statistics")
        result.merged = 0
        return result
    end

    result.watermark = tonumber(response.watermark)
    return result
end

function BridgeSync:_runStatisticsSync(stats_state)
    local payload, payload_err = self:_collectStatisticsPayload(stats_state)
    if not payload then
        error(payload_err or "Failed to build statistics payload")
    end

    local result = {
        skipped = #payload.page_stats == 0,
        device_key = payload.device_key,
        watermark = payload.watermark,
        db_fingerprint = payload.db_fingerprint,
        accepted_books = 0,
        accepted_page_stats = 0,
        duplicate_page_stats = 0,
        merged_page_stats = 0,
        merge_watermark = nil,
        merge_error = nil,
    }

    if not result.skipped then
        self:logInfo(
            "Uploading",
            #payload.page_stats,
            "reading stat rows in bounded batches"
        )

        for _, batch in ipairs(BridgeStatsBatches.build(
            payload.page_stats, payload.books, STATS_UPLOAD_BATCH
        )) do
            local ok, code, body = self.api:uploadStatistics({
                device = payload.device,
                device_id = payload.device_id,
                books = batch.books,
                page_stats = batch.page_stats,
            })
            if not ok then
                error(body or ("HTTP " .. tostring(code or "unknown")))
            end

            local parsed = {}
            if body and body ~= "" then
                local ok_json, decoded = pcall(json.decode, body)
                if ok_json and type(decoded) == "table" then parsed = decoded end
            end
            result.accepted_books = result.accepted_books
                + (tonumber(parsed.accepted_books) or #batch.books)
            result.accepted_page_stats = result.accepted_page_stats
                + (tonumber(parsed.accepted_page_stats) or #batch.page_stats)
            result.duplicate_page_stats = result.duplicate_page_stats
                + (tonumber(parsed.duplicate_page_stats) or 0)
        end
    end

    local merge = self:_mergeForeignStatistics(payload, stats_state)
    result.merged_page_stats = merge.merged or 0
    result.merge_watermark = merge.watermark
    result.merge_error = merge.err
    result.merge_backfill_pending = merge.backfill_pending
    if merge.err then
        self:logWarn("Cross-device stats merge failed:", merge.err)
    elseif result.merged_page_stats > 0 then
        self:logInfo("Merged", result.merged_page_stats, "reading stat rows from other devices")
    end

    return result
end

function BridgeSync:syncReadingStats(silent)
    if silent == nil then
        silent = false
    end

    if self.sync_in_progress then
        if not silent then
            self:_showMessage(_("Bridge Sync is already running"), 2)
        end
        return false
    end

    if not self.server_url or self.server_url == "" or
       not self.username or self.username == "" or
       not self.key or self.key == "" then
        if not silent then
            self:_showMessage(_("Bridge Sync is not configured"), 3)
        end
        return false
    end

    local network_ok, network_err = self:_preflightNetwork(silent)
    if not network_ok then
        self:logWarn(network_err)
        if not silent then
            self:_showMessage(network_err, 4)
        end
        return false
    end

    self.sync_in_progress = true
    local info_msg = nil
    if not silent then
        info_msg = InfoMessage:new{
            text = _("Syncing reading stats..."),
            timeout = 0,
        }
        UIManager:show(info_msg)
        UIManager:forceRePaint()
    end

    -- Snapshot persisted state in the parent: the sync runs in a forked
    -- child, and the SQLite state connection must never be used across a
    -- fork (inherited fd + POSIX locks).
    local stats_state = self:_snapshotStatsSyncState()

    local subprocess_ok, success, result = self:_runInSubprocess(function()
        return pcall(function()
            return self:_runStatisticsSync(stats_state)
        end)
    end)

    if info_msg then
        UIManager:close(info_msg)
    end
    self.sync_in_progress = false

    if not subprocess_ok then
        self:logErr("Reading stats sync subprocess failed", success or "")
        if not silent then
            self:_showMessage(T(_("Reading stats sync failed: %1"), tostring(success or "Subprocess failed")), 5)
        end
        return false
    end

    if not success then
        self:logErr(result or "Unknown reading stats sync error")
        if not silent then
            self:_showMessage(T(_("Reading stats sync failed: %1"), tostring(result or "Unknown error")), 5)
        end
        return false
    end

    if result.device_key and result.device_key ~= "" then
        self:_saveStateScalar("stats_last_uploaded_device_key", result.device_key)
    end
    if result.watermark and tonumber(result.watermark) then
        self:_saveStateScalar("stats_last_uploaded_start_time", tonumber(result.watermark))
    end
    if result.merge_watermark and tonumber(result.merge_watermark) then
        self:_saveStateScalar("stats_last_merged_watermark", tonumber(result.merge_watermark))
    end
    if result.merge_backfill_pending and not result.merge_error then
        self:_saveStateScalar("stats_merge_backfill_done", true)
    end

    -- Record the pre-collection fingerprint for differential tracking.
    -- Deliberately NOT re-read here: the foreign-stats merge above touches
    -- statistics.sqlite, and fingerprinting that post-merge mtime would mark
    -- sessions written during the sync as already uploaded.
    if result.db_fingerprint then
        self:_recordSyncFingerprint("bridge", "statistics", "*", result.db_fingerprint)
    end

    local message
    if result.skipped and (result.merged_page_stats or 0) == 0 then
        message = _("Reading stats are already up to date.")
    else
        message = T(
            _("Reading stats synced.\nAccepted rows: %1\nDuplicates: %2\nBooks: %3\nMerged from other devices: %4"),
            result.accepted_page_stats or 0,
            result.duplicate_page_stats or 0,
            result.accepted_books or 0,
            result.merged_page_stats or 0
        )
    end

    self:logInfo(message)
    if not silent then
        self:_showMessage(message, 5)
    end
    return true
end

function BridgeSync:onReaderReady()
    self:startSession()
    return false
end

function BridgeSync:onCloseDocument()
    local captured = self:_captureAnnotationSnapshot()
    local closed_file = nil
    if captured and captured.file then
        closed_file = captured.file
    elseif self.current_session and self.current_session.file_path then
        closed_file = self.current_session.file_path
    elseif self.ui and self.ui.document then
        closed_file = self.ui.document.file
    end
    self:endSession({ force_queue = false })
    self:_syncAnnotationsAfterClose(closed_file, captured)
    self:_scheduleBookSyncAfterClose(5)
    return false
end

function BridgeSync:_captureAnnotationSnapshot()
    if not self.is_enabled or not self.annotation_sync_enabled then
        return nil
    end
    -- Snapshot the live annotations NOW (plain copies, no reader references).
    -- The close uploader prefers the freshly flushed sidecar, but this snapshot
    -- is a fallback on builds where the sidecar is delayed or unavailable.
    local ok, captured = pcall(BridgeAnnotations.captureLiveBook, self.ui)
    if not ok or not captured then
        return nil
    end
    return captured
end

function BridgeSync:_savePendingAnnotationCloses()
    return self:_saveStateScalarJSON("pending_annotation_closes", self.pending_annotation_closes)
end

function BridgeSync:_enqueuePendingAnnotationClose(closed_file, captured)
    local item = {
        file = closed_file or (captured and captured.file),
        hash = captured and captured.hash or nil,
        annotations = captured and captured.annotations or nil,
        queued_at = os.time(),
    }
    if not item.hash and item.file then
        item.hash = BridgeAnnotations.resolveBookHash(item.file)
    end
    if not item.file and not item.hash then return false end

    local deduped = {}
    for _, pending in ipairs(self.pending_annotation_closes) do
        if not ((item.hash and pending.hash == item.hash) or
                (item.file and pending.file == item.file)) then
            deduped[#deduped + 1] = pending
        end
    end
    deduped[#deduped + 1] = item
    while #deduped > 10 do table.remove(deduped, 1) end
    self.pending_annotation_closes = deduped
    self:_savePendingAnnotationCloses()
    return true
end

function BridgeSync:_uploadPendingAnnotationCloses()
    local retained = {}
    local all_ok = true
    for _, pending in ipairs(self.pending_annotation_closes) do
        local book
        if pending.file then
            book = BridgeAnnotations.collectBookByFile(pending.file, pending.hash)
        end
        if not book and pending.hash and type(pending.annotations) == "table" then
            book = {
                file = pending.file,
                hash = pending.hash,
                annotations = pending.annotations,
                live = false,
            }
        end

        if book and book.hash then
            self:logInfo("Close-sync scanning highlights:",
                tostring(#(book.annotations or {})), "annotation(s)")
            local ok, result, err = pcall(
                BridgeAnnotations.exchangeBooks,
                self,
                { book },
                { upload_only = true }
            )
            if ok and type(result) == "table" then
                self:logInfo("Close-sync highlights:", tostring(result.uploaded), "uploaded,",
                    tostring(result.applied), "applied,", tostring(result.deleted), "deleted")
            else
                retained[#retained + 1] = pending
                all_ok = false
                self:logWarn("Close-sync highlights failed:", tostring(ok and err or result))
            end
        else
            self:logInfo("Close-sync discarded an unreadable snapshot")
        end
    end
    self.pending_annotation_closes = retained
    self:_savePendingAnnotationCloses()
    return all_ok
end

function BridgeSync:_maybeUploadPendingAnnotationCloses(reason)
    if #self.pending_annotation_closes == 0 then return false end
    if not NetworkMgr:isConnected() then
        self:logInfo("Close-sync highlights still queued after", tostring(reason or "close"),
            "- waiting for WiFi")
        return false
    end
    self:_submitSyncJob(
        "close_annotations",
        _("Close highlight sync"),
        reason or "close",
        BridgeSyncCoordinator.PRIORITY.lifecycle,
        true,
        function() return self:_uploadPendingAnnotationCloses() end
    )
    return true
end

function BridgeSync:_syncAnnotationsAfterClose(closed_file, captured)
    if not self.is_enabled or not self.annotation_sync_enabled then
        return
    end
    if not closed_file and not captured then
        return
    end
    self:_enqueuePendingAnnotationClose(closed_file, captured)
    self:_scheduleTask("close_annotations", 2, function()
        self:_maybeUploadPendingAnnotationCloses("close")
    end)
end

function BridgeSync:onSuspend()
    self:_cancelAutomaticTasks()
    if BridgeSweep.isRunning() then BridgeSweep.cancel() end
    self:_abandonRunningWork("suspend")
    self:endSession({ silent = true, force_queue = true })
    return false
end

-- Suspend and teardown must not leave work running. A queued job would resume
-- against a stale reader, and a forked child survives the suspend outright,
-- holding its share of memory and any socket it inherited until the next wake.
function BridgeSync:_abandonRunningWork(reason)
    local coordinator = self.sync_coordinator
    local dropped = 0
    if coordinator then
        dropped = coordinator:cancelPending()
        if coordinator:cancelActive() then
            dropped = dropped + 1
        end
    end
    local killed = 0
    for pid in pairs(live_subprocess_pids) do
        live_subprocess_pids[pid] = nil
        if pcall(FFIUtil.terminateSubProcess, pid) then
            killed = killed + 1
        end
    end
    if dropped > 0 or killed > 0 then
        self:logInfo("Abandoned background work on", tostring(reason),
            "- jobs:", tostring(dropped), "subprocesses:", tostring(killed))
    end
end

function BridgeSync:onCloseWidget()
    -- This instance is going away. Anything it still has queued would start
    -- against a torn down ReaderUI, so drop it. A job already running is left
    -- alone to finish -- only suspend stops work that is already in flight.
    if shared_coordinator then
        shared_coordinator:cancelPending(self)
    end
    return false
end

function BridgeSync:onBridgeSyncSyncBooks()
    self:_submitSyncJob(
        "books", _("Book sync"), "manual",
        BridgeSyncCoordinator.PRIORITY.manual, false,
        function() return self:syncFromBridge(false) end
    )
    return true
end

function BridgeSync:onBridgeSyncSyncStats()
    self:_submitSyncJob(
        "statistics", _("Reading stats sync"), "manual",
        BridgeSyncCoordinator.PRIORITY.manual, false,
        function() return self:syncReadingStats(false) end
    )
    return true
end

function BridgeSync:_maybeCheckForPluginUpdate()
    if self.server_url == "" or self.username == "" or self.key == "" then return end
    local last_check = tonumber(self:_getStateScalar("plugin_update_checked_at")) or 0
    if os.time() - last_check < 24 * 60 * 60 then return end
    self:_submitSyncJob(
        "plugin_update", _("Plugin update check"), "automatic",
        BridgeSyncCoordinator.PRIORITY.automatic, true,
        function() return self:checkForPluginUpdate(true) end
    )
end

function BridgeSync:checkForPluginUpdate(silent)
    if not self.server_url or self.server_url == "" then
        if not silent then self:_showMessage(_("Server URL is not configured"), 2) end
        return false
    end
    local network_ok, network_err = self:_preflightNetwork()
    if not network_ok then
        self:logWarn(network_err)
        if not silent then self:_showMessage(network_err, 4) end
        return false
    end

    local info_msg = nil
    if not silent then
        info_msg = InfoMessage:new{ text = _("Checking for plugin update..."), timeout = 0 }
        UIManager:show(info_msg)
        UIManager:forceRePaint()
    end

    local subprocess_ok, ok, result = self:_runInSubprocess(function()
        return self.api:getPluginVersion()
    end, { inline = true })

    if info_msg then UIManager:close(info_msg) end

    if not subprocess_ok then
        self:logErr("Plugin version check subprocess failed", ok or "")
        if not silent then self:_showMessage(T(_("Plugin version check failed: %1"), tostring(ok or "Subprocess failed")), 5) end
        return false
    end

    if not ok then
        self:logWarn(result or "Version check failed")
        if not silent then self:_showMessage(result or _("Version check failed"), 4) end
        return false
    end

    local remote_version = type(result) == "table" and result.version or nil
    if not remote_version then
        if not silent then self:_showMessage(_("Invalid version response from server"), 4) end
        return false
    end

    local local_version = "unknown"
    local chunk = loadfile(self.path .. "/_meta.lua")
    if chunk then
        local meta_ok, meta_table = pcall(chunk)
        if meta_ok and type(meta_table) == "table" and meta_table.version then
            local_version = meta_table.version
        end
    end

    self:_saveStateScalar("plugin_update_checked_at", os.time())
    self:_saveStateScalar("plugin_update_latest_version", remote_version)

    if not BridgeVersion.isNewer(remote_version, local_version) then
        if not silent then self:_showMessage(T(_("Plugin is up to date (v%1)"), local_version), 3) end
        return true
    end

    if silent then
        self:logInfo("Plugin update available:", local_version, "to", remote_version)
        return true
    end

    UIManager:show(ConfirmBox:new{
        text = T(
            _("Update plugin from v%1 to v%2?\nKOReader will need to restart."),
            local_version,
            remote_version
        ),
        ok_text = _("Update"),
        cancel_text = _("Cancel"),
        ok_callback = function()
            Trapper:wrap(function()
                self:_downloadAndInstallPlugin(remote_version)
            end)
        end,
    })
    return true
end

-- Wraps a path in POSIX single quotes, escaping embedded single quotes.
local function _shellQuote(path)
    return "'" .. tostring(path):gsub("'", "'\\''") .. "'"
end

local function _extractWithArchiver(Archiver, zip_path, staging)
    local archive = Archiver.Reader:new()
    if not archive:open(zip_path) then
        local open_err = archive.err
        archive:close()
        return nil, tostring(open_err or "could not open update archive")
    end
    for entry in archive:iterate() do
        local path = tostring(entry.path or "")
        if path == "" or path:sub(1, 1) == "/" or path == ".."
            or path:sub(1, 3) == "../" or path:find("/../", 1, true) then
            archive:close()
            return nil, "unsafe path in update archive"
        end
        -- A failed entry must abort the whole update. Breaking out and returning
        -- success left a PARTIAL plugin staged; _installPluginZip only checks that
        -- _meta.lua and main.lua exist, so a tree missing any other module passed
        -- every gate and was renamed into place over the backup -- bricking the
        -- plugin at the next require().
        if not archive:extractToPath(path, staging .. "/" .. path) then
            local entry_err = archive.err
            archive:close()
            return nil, tostring(entry_err or ("could not extract " .. path))
        end
    end
    local extract_err = archive.err
    archive:close()
    if extract_err then return nil, tostring(extract_err) end
    return true
end

BridgeSync._extractWithArchiver = _extractWithArchiver

local function _verifyPluginArchive(zip_path, expected_digest, log)
    if not expected_digest or expected_digest == "" then
        -- Older bridges do not send X-Content-SHA256, and some reverse proxies strip
        -- unknown X- response headers. Continuing is deliberate (the update would
        -- otherwise be impossible), but it must not look like the archive was
        -- checked when it was not.
        if log then
            log("warn", "Update archive checksum unavailable - installing without SHA-256 verification")
        end
        return true
    end
    expected_digest = tostring(expected_digest)
    if #expected_digest ~= 64 or not expected_digest:match("^%x+$") then
        return nil, "server returned an invalid update checksum"
    end
    if type(sha2.sha256) ~= "function" then
        return nil, "SHA-256 verification is unavailable on this device"
    end
    local handle, open_err = io.open(zip_path, "rb")
    if not handle then return nil, tostring(open_err or "could not read update archive") end
    local contents = handle:read("*a")
    handle:close()
    local ok, actual = pcall(sha2.sha256, contents)
    if not ok or not actual then return nil, "could not calculate update checksum" end
    if tostring(actual):lower() ~= expected_digest:lower() then
        return nil, "downloaded update checksum mismatch"
    end
    return true
end

BridgeSync._verifyPluginArchive = _verifyPluginArchive

local function _validatePluginMetadata(meta_path, expected_version)
    local handle, open_err = io.open(meta_path, "rb")
    if not handle then return nil, tostring(open_err or "could not read plugin metadata") end
    local contents = handle:read("*a")
    handle:close()
    local function field(name)
        local prefix = "[%s,{]" .. name .. "%s*=%s*"
        return contents:match(prefix .. "\"([^\"]+)\"")
            or contents:match(prefix .. "'([^']+)'")
    end
    if field("name") ~= "bridgesync" then return nil, "update archive is not BridgeSync" end
    local version = field("version")
    if not version then return nil, "update archive has no plugin version" end
    if expected_version and tostring(version) ~= tostring(expected_version) then
        return nil, "update archive version does not match the offered version"
    end
    return true
end

BridgeSync._validatePluginMetadata = _validatePluginMetadata

function BridgeSync:_downloadAndInstallPlugin(version)
    local temp_path = DataStorage:getSettingsDir() .. "/bridgesync-update.zip"

    local info_msg = InfoMessage:new{
        text = T(_("Downloading plugin v%1..."), version),
        timeout = 0,
    }
    UIManager:show(info_msg)
    UIManager:forceRePaint()

    local subprocess_ok, ok, digest_or_err = self:_runInSubprocess(function()
        return self.api:downloadPluginZip(temp_path)
    end)

    UIManager:close(info_msg)

    if not subprocess_ok then
        self:logErr("Plugin download subprocess failed", ok or "")
        self:_showMessage(T(_("Plugin download failed: %1"), tostring(ok or "Subprocess failed")), 5)
        return
    end

    if not ok then
        self:logWarn(digest_or_err or "Download failed")
        self:_showMessage(digest_or_err or _("Plugin download failed"), 4)
        return
    end

    -- Atomic install: extract into a staging directory (a partial unpack never
    -- touches the live plugin), back up the current plugin dir, rename the new
    -- one into place, and restore the backup on any failure. Uses KOReader's
    -- archive helper — platform `unzip` exit codes vary and some devices don't
    -- ship unzip at all. Staging/backup live next to the plugin dir so the
    -- renames stay on one filesystem.
    local install_ok, install_err = self:_installPluginZip(temp_path, version, digest_or_err)
    os.remove(temp_path)

    if not install_ok then
        self:logErr("Plugin update failed:", tostring(install_err))
        self:_showMessage(T(_("Plugin update failed: %1"), tostring(install_err)), 6)
        return
    end

    UIManager:show(ConfirmBox:new{
        text = T(_("Plugin updated to v%1. Restart KOReader now?"), version),
        ok_text = _("Restart Now"),
        cancel_text = _("Later"),
        ok_callback = function()
            UIManager:quit(UIManager.RETURN_CODE_REBOOT or 85)
        end,
    })
end

function BridgeSync:_installPluginZip(zip_path, expected_version, expected_digest)
    local plugin_dir = tostring(self.path):gsub("/+$", "")
    local plugins_dir = plugin_dir:match("^(.+)/[^/]+$")
    local plugin_name = plugin_dir:match("([^/]+)$")
    if not plugins_dir or not plugin_name then
        return nil, "cannot determine plugin directory layout"
    end

    local staging = plugins_dir .. "/" .. plugin_name .. ".update"
    local backup = plugins_dir .. "/" .. plugin_name .. ".bak"

    local digest_ok, digest_err = _verifyPluginArchive(
        zip_path, expected_digest,
        function(level, message) self:_appendLog(level, message) end
    )
    if not digest_ok then return nil, digest_err end

    -- Clear any leftovers from a previously interrupted attempt.
    os.execute("rm -rf " .. _shellQuote(staging))

    if not lfs.mkdir(staging) and lfs.attributes(staging, "mode") ~= "directory" then
        return nil, "could not create staging directory"
    end

    local unpack_ok, unpack_err
    local has_archiver, Archiver = pcall(require, "ffi/archiver")
    if has_archiver and type(Archiver) == "table" and Archiver.Reader then
        unpack_ok, unpack_err = _extractWithArchiver(Archiver, zip_path, staging)
    else
        local Device = require("device")
        if type(Device.unpackArchive) == "function" then
            unpack_ok, unpack_err = Device:unpackArchive(zip_path, staging, true)
        else
            local exit_code = os.execute(
                "unzip -o " .. _shellQuote(zip_path) .. " -d " .. _shellQuote(staging) .. " >/dev/null 2>&1"
            )
            unpack_ok = (exit_code == 0 or exit_code == true)
            unpack_err = unpack_ok and nil or "unzip failed"
        end
    end
    if not unpack_ok then
        os.execute("rm -rf " .. _shellQuote(staging))
        return nil, tostring(unpack_err or "archive extraction failed")
    end

    -- Locate the extracted plugin root by finding _meta.lua, rather than
    -- assuming a layout: KOReader's unpackArchive may strip the top folder
    -- (files land directly in staging) or keep it (staging/<name>/…), and it
    -- varies by version. This is version-proof either way.
    local staged_plugin
    if lfs.attributes(staging .. "/_meta.lua", "mode") == "file" then
        staged_plugin = staging  -- top folder was stripped
    else
        for entry in lfs.dir(staging) do
            if entry ~= "." and entry ~= ".." then
                local candidate = staging .. "/" .. entry
                if lfs.attributes(candidate, "mode") == "directory"
                    and lfs.attributes(candidate .. "/_meta.lua", "mode") == "file" then
                    staged_plugin = candidate
                    break
                end
            end
        end
    end
    if not staged_plugin then
        os.execute("rm -rf " .. _shellQuote(staging))
        return nil, "downloaded archive has no _meta.lua"
    end
    if lfs.attributes(staged_plugin .. "/main.lua", "mode") ~= "file" then
        os.execute("rm -rf " .. _shellQuote(staging))
        return nil, "downloaded archive has no main.lua"
    end
    local metadata_ok, metadata_err = _validatePluginMetadata(
        staged_plugin .. "/_meta.lua", expected_version)
    if not metadata_ok then
        os.execute("rm -rf " .. _shellQuote(staging))
        return nil, metadata_err
    end

    os.execute("rm -rf " .. _shellQuote(backup))
    local renamed_away, rename_err = os.rename(plugin_dir, backup)
    if not renamed_away then
        os.execute("rm -rf " .. _shellQuote(staging))
        return nil, "could not move current plugin aside: " .. tostring(rename_err)
    end

    local installed, install_err = os.rename(staged_plugin, plugin_dir)
    if not installed then
        -- Restore the original so the user always has a working plugin.
        os.rename(backup, plugin_dir)
        os.execute("rm -rf " .. _shellQuote(staging))
        return nil, "could not install new plugin: " .. tostring(install_err)
    end

    os.execute("rm -rf " .. _shellQuote(staging))
    os.execute("rm -rf " .. _shellQuote(backup))
    return true
end

function BridgeSync:addToMainMenu(menu_items)
    menu_items.bridge_sync = {
        text = _("Bridge Sync"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("Enable Sync"),
                keep_menu_open = true,
                checked_func = function()
                    return self.is_enabled
                end,
                callback = function(touchmenu_instance)
                    self.is_enabled = not self.is_enabled
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                    self:_showMessage(
                        self.is_enabled and _("Bridge Sync enabled") or _("Bridge Sync disabled"),
                        2
                    )
                end,
            },
            {
                text = _("Sync Now"),
                callback = function()
                    self:onBridgeSyncSyncBooks()
                end,
            },
            {
                text = _("Sync Reading Stats"),
                callback = function()
                    self:onBridgeSyncSyncStats()
                end,
            },
            {
                text = _("Sync Highlights"),
                callback = function()
                    self:onBridgeSyncSyncAnnotations()
                end,
            },
            {
                text_func = function()
                    local status = self.sync_coordinator:status()
                    if status.current then
                        return T(_("Sync Status: %1 (%2 queued)"), status.current.label, status.pending_count)
                    end
                    return T(_("Sync Status: Idle (%1 queued)"), status.pending_count)
                end,
                callback = function()
                    self:_showMessage(self:_syncStatusSummary(), 6)
                end,
            },
            {
                text_func = function()
                    return BridgeSweep.isRunning()
                        and _("Cancel Highlight Sweep")
                        or _("Sweep All Highlights")
                end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    if BridgeSweep.isRunning() then
                        BridgeSweep.cancel()
                        self:_showMessage(_("Cancelling highlight sweep…"), 2)
                    else
                        self:startAnnotationSweep()
                    end
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text = _("Manual Only"),
                keep_menu_open = true,
                checked_func = function()
                    return self.manual_only
                end,
                callback = function(touchmenu_instance)
                    self.manual_only = not self.manual_only
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text = _("Auto-Sync on Wake"),
                keep_menu_open = true,
                checked_func = function()
                    return self.auto_sync_on_resume
                end,
                callback = function(touchmenu_instance)
                    self.auto_sync_on_resume = not self.auto_sync_on_resume
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text = _("Auto-Sync on Network"),
                keep_menu_open = true,
                checked_func = function()
                    return self.auto_sync_on_network
                end,
                callback = function(touchmenu_instance)
                    self.auto_sync_on_network = not self.auto_sync_on_network
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text = _("Auto-Sync Books on Close"),
                keep_menu_open = true,
                checked_func = function()
                    return self.auto_sync_on_close
                end,
                callback = function(touchmenu_instance)
                    self.auto_sync_on_close = not self.auto_sync_on_close
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text = _("Do Not Sync While Reading"),
                keep_menu_open = true,
                checked_func = function()
                    return self.do_not_sync_while_book_open
                end,
                callback = function(touchmenu_instance)
                    self.do_not_sync_while_book_open = not self.do_not_sync_while_book_open
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text = _("Delete Removed Books"),
                keep_menu_open = true,
                checked_func = function()
                    return self.delete_removed_books
                end,
                callback = function(touchmenu_instance)
                    self.delete_removed_books = not self.delete_removed_books
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text_func = function()
                    return T(_("Max Downloads per Sync: %1"), self.max_downloads_per_sync == 0 and _("Unlimited") or self.max_downloads_per_sync)
                end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self:_promptForSetting(
                        _("Max Downloads per Sync"),
                        tostring(self.max_downloads_per_sync),
                        _("Enter max downloads (0 = unlimited)"),
                        function(value)
                            local n = tonumber(value)
                            if not n or n <= 0 then
                                self.max_downloads_per_sync = 0
                            else
                                self.max_downloads_per_sync = math.floor(n)
                            end
                            self:_saveSettings()
                        end,
                        nil,
                        function()
                            self:_refreshMenu(touchmenu_instance)
                        end
                    )
                end,
            },
            {
                text = _("Track Reading Sessions"),
                keep_menu_open = true,
                checked_func = function()
                    return self.session_tracking_enabled
                end,
                callback = function(touchmenu_instance)
                    self.session_tracking_enabled = not self.session_tracking_enabled
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text = _("Auto-Sync Reading Stats"),
                keep_menu_open = true,
                enabled_func = function()
                    return self.session_tracking_enabled
                end,
                checked_func = function()
                    return self.auto_sync_stats
                end,
                callback = function(touchmenu_instance)
                    self.auto_sync_stats = not self.auto_sync_stats
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text = _("Sync Highlights & Notes"),
                keep_menu_open = true,
                checked_func = function()
                    return self.annotation_sync_enabled
                end,
                callback = function(touchmenu_instance)
                    self.annotation_sync_enabled = not self.annotation_sync_enabled
                    self:_saveSettings()
                    self:_refreshMenu(touchmenu_instance)
                end,
            },
            {
                text_func = function()
                    return T(_("Pending Sessions: %1"), #self.pending_sessions)
                end,
                enabled_func = function()
                    return #self.pending_sessions > 0
                end,
                callback = function()
                    if #self.pending_sessions > 0 and NetworkMgr:isConnected() then
                        self:_submitSyncJob(
                            "sessions", _("Reading session upload"), "manual",
                            BridgeSyncCoordinator.PRIORITY.manual, false,
                            function()
                                local pending_count = #self.pending_sessions
                                local ok = self:_uploadSessions()
                                if ok then self:_showMessage(T(_("Uploaded %1 session(s)"), pending_count), 2) end
                                return ok
                            end
                        )
                    elseif #self.pending_sessions > 0 then
                        self:_showMessage(_("No network connection"), 2)
                    end
                end,
            },
            {
                text_func = function()
                    return T(_("Server URL: %1"), self.server_url ~= "" and self.server_url or _("Not set"))
                end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self:_promptForSetting(
                        _("Bridge Server URL"),
                        self.server_url,
                        _("Enter bridge base URL"),
                        function(value)
                            self.server_url = value
                            self:_saveSettings()
                        end,
                        nil,
                        function()
                            self:_refreshMenu(touchmenu_instance)
                        end
                    )
                end,
            },
            {
                text_func = function()
                    return T(_("Username: %1"), self.username ~= "" and self.username or _("Not set"))
                end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self:_promptForSetting(
                        _("Bridge Username"),
                        self.username,
                        _("Enter KOSync username"),
                        function(value)
                            self.username = value
                            self:_saveSettings()
                        end,
                        nil,
                        function()
                            self:_refreshMenu(touchmenu_instance)
                        end
                    )
                end,
            },
            {
                text = _("Configure Key"),
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self:_promptForSetting(
                        _("Bridge Key"),
                        self.key,
                        _("Enter KOSync key"),
                        function(value)
                            self.key = value
                            self:_saveSettings()
                        end,
                        true,
                        function()
                            self:_refreshMenu(touchmenu_instance)
                        end
                    )
                end,
            },
            {
                text_func = function()
                    return T(_("Wake Sync Delay: %1s"), self.wake_sync_delay_seconds)
                end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self:_promptForSetting(
                        _("Wake Sync Delay"),
                        tostring(self.wake_sync_delay_seconds),
                        _("Enter delay in seconds"),
                        function(value)
                            local delay = tonumber(value)
                            if delay and delay >= 5 then
                                self.wake_sync_delay_seconds = math.floor(delay)
                                self:_saveSettings()
                            else
                                self:_showMessage(_("Wake Sync Delay must be at least 5 seconds"), 3)
                            end
                        end,
                        nil,
                        function()
                            self:_refreshMenu(touchmenu_instance)
                        end
                    )
                end,
            },
            {
                text_func = function()
                    return T(_("Managed Folder: %1"), self.download_dir)
                end,
                keep_menu_open = true,
                callback = function(touchmenu_instance)
                    self:_showManagedFolderChooser(touchmenu_instance)
                end,
            },
            {
                text = _("Test Connection"),
                callback = function()
                    Trapper:wrap(function()
                        self:testConnection()
                    end)
                end,
            },
            {
                text_func = function()
                    local latest = self:_getStateScalar("plugin_update_latest_version")
                    if latest then
                        return T(_("Check for Plugin Update (latest: v%1)"), tostring(latest))
                    end
                    return _("Check for Plugin Update")
                end,
                callback = function()
                    self:_submitSyncJob(
                        "plugin_update", _("Plugin update check"), "manual",
                        BridgeSyncCoordinator.PRIORITY.manual, false,
                        function() return self:checkForPluginUpdate(false) end
                    )
                end,
            },
        },
    }
end

return BridgeSync
