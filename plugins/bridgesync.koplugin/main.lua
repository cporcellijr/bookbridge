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
local md5 = require("ffi/sha2").md5
local FFIUtil = require("ffi/util")
local buffer = require("string.buffer")
local socket = require("socket")
local json = require("json")
local APIClient = require("bridge_api_client")
local BridgeAnnotations = require("bridge_annotations")
local BridgeSweep = require("bridge_sweep")
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

local function _(text)
    return text
end
local T = require("ffi/util").template

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

    self.server_url = self.settings:readSetting("server_url") or ""
    self.username = self.settings:readSetting("username") or ""
    self.key = self.settings:readSetting("key") or ""
    self.download_dir = self.settings:readSetting("download_dir") or self:_detectDefaultDownloadDir()
    self.is_enabled = self.settings:readSetting("is_enabled") or false
    self.auto_sync_on_resume = self.settings:readSetting("auto_sync_on_resume") or false
    self.auto_sync_on_network = self.settings:readSetting("auto_sync_on_network") or false
    self.delete_removed_books = self.settings:readSetting("delete_removed_books") or false
    self.manual_only = self.settings:readSetting("manual_only") or false
    local do_not_sync_while_book_open = self.settings:readSetting("do_not_sync_while_book_open")
    if do_not_sync_while_book_open == nil then
        self.do_not_sync_while_book_open = true
    else
        self.do_not_sync_while_book_open = do_not_sync_while_book_open
    end
    self.wake_sync_delay_seconds = tonumber(self.settings:readSetting("wake_sync_delay_seconds")) or 30

    -- Reading session tracking
    local session_tracking = self.settings:readSetting("session_tracking_enabled")
    if session_tracking == nil then
        self.session_tracking_enabled = true
    else
        self.session_tracking_enabled = session_tracking
    end
    self.min_session_duration = tonumber(self.settings:readSetting("min_session_duration")) or 30
    local auto_sync_stats = self.settings:readSetting("auto_sync_stats")
    if auto_sync_stats == nil then
        self.auto_sync_stats = true
    else
        self.auto_sync_stats = auto_sync_stats
    end
    local annotation_sync = self.settings:readSetting("annotation_sync_enabled")
    if annotation_sync == nil then
        self.annotation_sync_enabled = true
    else
        self.annotation_sync_enabled = annotation_sync
    end
    self.current_session = nil
    self.pending_sessions = self.state:readSetting("pending_sessions") or {}

    self.sync_in_progress = false
    self.last_auto_sync_time = 0
    self.last_stats_sync_time = 0
    self.stats_sync_in_flight = false
    self.needs_wake_sync = false
    self.sync_scheduled = false
    self.log_path = DataStorage:getSettingsDir() .. "/bridge_sync.log"

    self.api = APIClient:new()
    self.api:init(self.server_url, self.username, self.key, function(level, message)
        self:_appendLog(level, message)
    end)

    self.ui.menu:registerToMainMenu(self)
end

function BridgeSync:_appendLog(level, message)
    local line = os.date("%Y-%m-%d %H:%M:%S") .. " [" .. tostring(level or "info") .. "] " .. tostring(message or "") .. "\n"
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

function BridgeSync:_detectDefaultDownloadDir()
    if lfs.attributes("/mnt/onboard", "mode") == "directory" then
        return "/mnt/onboard/Books/BridgeManaged"
    elseif lfs.attributes("/sdcard", "mode") == "directory" then
        return "/sdcard/Books/BridgeManaged"
    end
    return "/Books/BridgeManaged"
end

function BridgeSync:_saveSettings()
    self.settings:saveSetting("server_url", self.server_url)
    self.settings:saveSetting("username", self.username)
    self.settings:saveSetting("key", self.key)
    self.settings:saveSetting("download_dir", self.download_dir)
    self.settings:saveSetting("is_enabled", self.is_enabled)
    self.settings:saveSetting("auto_sync_on_resume", self.auto_sync_on_resume)
    self.settings:saveSetting("auto_sync_on_network", self.auto_sync_on_network)
    self.settings:saveSetting("delete_removed_books", self.delete_removed_books)
    self.settings:saveSetting("manual_only", self.manual_only)
    self.settings:saveSetting("do_not_sync_while_book_open", self.do_not_sync_while_book_open)
    self.settings:saveSetting("wake_sync_delay_seconds", self.wake_sync_delay_seconds)
    self.settings:saveSetting("session_tracking_enabled", self.session_tracking_enabled)
    self.settings:saveSetting("min_session_duration", self.min_session_duration)
    self.settings:saveSetting("auto_sync_stats", self.auto_sync_stats)
    self.settings:saveSetting("annotation_sync_enabled", self.annotation_sync_enabled)
    self.settings:flush()
    self.api:init(self.server_url, self.username, self.key, function(level, message)
        self:_appendLog(level, message)
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
    return self.state:readSetting("items") or {}
end

function BridgeSync:_saveState(items, revision)
    self.state:saveSetting("items", items or {})
    self.state:saveSetting("revision", revision or "")
    self.state:flush()
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

function BridgeSync:_runInSubprocess(task)
    local co = coroutine.running()
    if not co then
        return true, task()
    end

    local pid, parent_read_fd = FFIUtil.runInSubProcess(function(_, child_write_fd)
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
                    return false, decoded or "malformed subprocess result"
                end
                if not subprocess_done then
                    local collect_and_clean
                    collect_and_clean = function()
                        if FFIUtil.isSubProcessDone(pid) then
                            logger.dbg("Bridge Sync subprocess collected")
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

    if ret_values then
        return true, table.unpack(ret_values, 1, ret_values.n or #ret_values)
    end
    return true
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
        return false
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
        if lfs.attributes(partial, "mode") ~= "directory" then
            local ok = lfs.mkdir(partial)
            if not ok and lfs.attributes(partial, "mode") ~= "directory" then
                return false
            end
        end
    end
    return lfs.attributes(normalized, "mode") == "directory"
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
    self.needs_wake_sync = false
    self.last_auto_sync_time = os.time()  -- cooldown only once we actually sync
    Trapper:wrap(function()
        self:syncFromBridge(silent == nil and true or silent)
    end)
end

function BridgeSync:_scheduleSync(delay_seconds, silent, retries_left)
    if self.sync_scheduled then
        return
    end

    self.sync_scheduled = true
    UIManager:scheduleIn(delay_seconds or 10, function()
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
    self:_uploadSessions()
    return true
end

function BridgeSync:_maybeAutoSyncStats(reason)
    if not self.is_enabled or not self.session_tracking_enabled or not self.auto_sync_stats then
        return false
    end
    if self.stats_sync_in_flight then
        return false
    end
    if not NetworkMgr:isConnected() then
        return false
    end
    if (os.time() - (self.last_stats_sync_time or 0)) < 300 then
        return false
    end
    if reason then
        self:logInfo("Auto-syncing reading stats after", reason)
    end
    self.stats_sync_in_flight = true
    Trapper:wrap(function()
        local ok = self:syncReadingStats(true)
        self.stats_sync_in_flight = false
        -- Only start the 5-minute cooldown once a sync actually succeeds, so a failed attempt
        -- right after wake doesn't block the next reconnect from retrying.
        if ok then
            self.last_stats_sync_time = os.time()
        end
        -- Highlights ride the same cadence: exchange after each stats round.
        self:syncAnnotations(true)
    end)
    return true
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
    Trapper:wrap(function()
        self:syncAnnotations(false)
    end)
    return true
end

function BridgeSync:startAnnotationSweep()
    if not self.is_enabled or not self.annotation_sync_enabled then
        self:_showMessage(_("Highlight sync is disabled"), 3)
        return
    end
    if not self.server_url or self.server_url == "" or
       not self.username or self.username == "" or
       not self.key or self.key == "" then
        self:_showMessage(_("Bridge Sync is not configured"), 3)
        return
    end
    if BridgeSweep.isRunning() then
        self:_showMessage(_("Highlight sweep is already running"), 3)
        return
    end
    if not NetworkMgr:isConnected() then
        self:_showMessage(_("No network connection"), 3)
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
        end
    )
    if started then
        self:_showMessage(_("Highlight sweep started in the background."), 3)
    else
        self:_showMessage(T(_("Highlight sweep not started: %1"), tostring(err)), 4)
    end
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

    local hash_cache = self.state:readSetting("hash_cache") or {}
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

    self.state:saveSetting("hash_cache", new_cache)
    self.state:flush()
    return index
end

function BridgeSync:_findTrackedAbsIdByPath(items, path)
    for abs_id, entry in pairs(items) do
        if entry.local_path == path then
            return abs_id
        end
    end
    return nil
end

function BridgeSync:_safeRemove(path)
    if path and self:_fileExists(path) then
        os.remove(path)
    end
end

function BridgeSync:_removeTree(path)
    local mode = lfs.attributes(path, "mode")
    if mode == "file" then
        os.remove(path)
        return true
    end
    if mode ~= "directory" then
        return false
    end

    for entry in lfs.dir(path) do
        if entry ~= "." and entry ~= ".." then
            local child = path .. "/" .. entry
            self:_removeTree(child)
        end
    end
    lfs.rmdir(path)
    return true
end

function BridgeSync:_deleteManagedFile(path)
    if not path or path == "" then
        return
    end
    self:_safeRemove(path)
    self:_removeTree(path .. ".sdr")
end

function BridgeSync:_moveFile(source_path, target_path)
    if source_path == target_path then
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
    return current and path and current == path
end

function BridgeSync:_runSync()
    if not self:_ensureDirectory(self.download_dir) then
        error("Failed to create managed folder")
    end

    local local_revision = tostring(self.state:readSetting("revision") or "")
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
            revision = remote_revision,
            unchanged = true,
        }
    end

    local remote_books = manifest.books or {}
    local remote_by_abs = {}
    local items = self:_loadStateItems()
    local hash_index = nil
    local function getHashIndex()
        if not hash_index then
            hash_index = self:_buildHashIndex()
        end
        return hash_index
    end
    local downloaded, skipped, renamed, deleted, deferred, errors = 0, 0, 0, 0, 0, 0

    for _, book in ipairs(remote_books) do
        remote_by_abs[book.abs_id] = true
        local target_path = self.download_dir .. "/" .. book.filename
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
            if reused_path ~= target_path then
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
            local temp_path = target_path .. ".part"
            self:_safeRemove(temp_path)
            local dl_ok, dl_err = self.api:downloadBook(book.download_path, temp_path)
            if not dl_ok then
                self:logWarn("Download failed for", book.abs_id, dl_err or "")
                errors = errors + 1
                self:_safeRemove(temp_path)
            else
                local downloaded_hash = self:_calculateBookHash(temp_path)
                if downloaded_hash and downloaded_hash ~= book.content_hash then
                    self:logWarn("Hash mismatch for", book.abs_id, downloaded_hash, book.content_hash)
                    errors = errors + 1
                    self:_safeRemove(temp_path)
                else
                    self:_safeRemove(target_path)
                    local move_ok, move_err = os.rename(temp_path, target_path)
                    if not move_ok then
                        self:logWarn("Rename failed for", book.abs_id, move_err or "")
                        errors = errors + 1
                        self:_safeRemove(temp_path)
                    else
                        downloaded = downloaded + 1
                        if previous_entry
                            and previous_entry.local_path == target_path
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

    if self.delete_removed_books then
        for abs_id, entry in pairs(items) do
            if not remote_by_abs[abs_id] then
                if self:_isCurrentDocument(entry.local_path) then
                    entry.pending_delete = true
                    items[abs_id] = entry
                    deferred = deferred + 1
                else
                    self:_deleteManagedFile(entry.local_path)
                    items[abs_id] = nil
                    deleted = deleted + 1
                end
            elseif entry.pending_delete then
                entry.pending_delete = nil
                items[abs_id] = entry
            end
        end
    end

    self:_updateCollections(items)
    self:_saveState(items, manifest.revision or "")
    return {
        downloaded = downloaded,
        skipped = skipped,
        renamed = renamed,
        deleted = deleted,
        deferred = deferred,
        errors = errors,
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
    end)

    if info_msg then
        UIManager:close(info_msg)
    end
    self.sync_in_progress = false

    if not subprocess_ok then
        self:logErr("Bridge Sync subprocess failed", success or "")
        if not silent then
            self:_showMessage(T(_("Bridge Sync failed: %1"), tostring(success or "Subprocess failed")), 5)
        end
        return false
    end

    if not success then
        self:logErr(result or "Unknown sync error")
        if not silent then
            self:_showMessage(T(_("Bridge Sync failed: %1"), tostring(result or "Unknown error")), 5)
        end
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
    end)

    UIManager:close(info_msg)

    if not subprocess_ok then
        self:logErr("Bridge connection test subprocess failed", ok or "")
        self:_showMessage(T(_("Bridge connection test failed: %1"), tostring(ok or "Subprocess failed")), 5)
        return
    end

    if ok then
        self:_showMessage(_("Authentication successful"), 2)
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
        if entry.local_path and entry.local_path == file_path then
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
    }

    table.insert(self.pending_sessions, session)
    self.state:saveSetting("pending_sessions", self.pending_sessions)
    self.state:flush()

    self:logInfo("Session ended:", duration_seconds, "s,", pages_read, "pages,",
        self.current_session.start_progress, "% ->", end_progress, "%")

    self.current_session = nil

    if not force_queue and NetworkMgr:isConnected() then
        self:_uploadSessions()
    end
end

function BridgeSync:_uploadSessions()
    if #self.pending_sessions == 0 then
        return
    end

    self:logInfo("Uploading", #self.pending_sessions, "pending sessions")

    local ok, code, body = self.api:uploadSessions(self.pending_sessions)
    if ok then
        self:logInfo("Sessions uploaded successfully")
        self.pending_sessions = {}
        self.state:saveSetting("pending_sessions", self.pending_sessions)
        self.state:flush()
    else
        self:logWarn("Session upload failed:", code or "", body or "", "- will retry later")
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

function BridgeSync:_collectStatisticsPayload()
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
    local last_uploaded_device_key = tostring(self.state:readSetting("stats_last_uploaded_device_key") or "")
    local last_uploaded_start_time = tonumber(self.state:readSetting("stats_last_uploaded_start_time")) or 0
    if last_uploaded_device_key ~= "" and last_uploaded_device_key ~= device_key then
        last_uploaded_start_time = 0
    end
    local replay_from = math.max(0, math.floor(last_uploaded_start_time - 300))

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

function BridgeSync:_mergeForeignStatistics(payload)
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

    local since = tonumber(self.state:readSetting("stats_last_merged_watermark")) or 0
    if not self.state:readSetting("stats_merge_backfill_done") then
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

function BridgeSync:_runStatisticsSync()
    local payload, payload_err = self:_collectStatisticsPayload()
    if not payload then
        error(payload_err or "Failed to build statistics payload")
    end

    local result = {
        skipped = #payload.page_stats == 0,
        device_key = payload.device_key,
        watermark = payload.watermark,
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
            "reading stat rows and",
            #payload.books,
            "book metadata rows"
        )

        local ok, code, body = self.api:uploadStatistics({
            device = payload.device,
            device_id = payload.device_id,
            books = payload.books,
            page_stats = payload.page_stats,
        })
        if not ok then
            error(body or ("HTTP " .. tostring(code or "unknown")))
        end

        local parsed = {}
        if body and body ~= "" then
            local ok_json, decoded = pcall(json.decode, body)
            if ok_json and type(decoded) == "table" then
                parsed = decoded
            end
        end

        result.accepted_books = tonumber(parsed.accepted_books) or #payload.books
        result.accepted_page_stats = tonumber(parsed.accepted_page_stats) or #payload.page_stats
        result.duplicate_page_stats = tonumber(parsed.duplicate_page_stats) or 0
    end

    local merge = self:_mergeForeignStatistics(payload)
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

    local subprocess_ok, success, result = self:_runInSubprocess(function()
        return pcall(function()
            return self:_runStatisticsSync()
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
        self.state:saveSetting("stats_last_uploaded_device_key", result.device_key)
    end
    if result.watermark and tonumber(result.watermark) then
        self.state:saveSetting("stats_last_uploaded_start_time", tonumber(result.watermark))
    end
    if result.merge_watermark and tonumber(result.merge_watermark) then
        self.state:saveSetting("stats_last_merged_watermark", tonumber(result.merge_watermark))
    end
    if result.merge_backfill_pending and not result.merge_error then
        self.state:saveSetting("stats_merge_backfill_done", true)
    end
    self.state:flush()

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
    self:endSession({ force_queue = false })
    self:_captureAnnotationsOnClose()
    return false
end

function BridgeSync:_captureAnnotationsOnClose()
    if not self.is_enabled or not self.annotation_sync_enabled then
        return
    end
    -- Snapshot the live annotations NOW (plain copies, no reader references);
    -- upload after the close path has finished so applying server-side changes
    -- merges into the freshly flushed sidecar.
    local ok, captured = pcall(BridgeAnnotations.captureLiveBook, self.ui)
    if not ok or not captured then
        return
    end
    UIManager:scheduleIn(2, function()
        if self.annotation_close_sync_in_flight then
            return
        end
        if not NetworkMgr:isConnected() then
            return -- silent; the periodic sync catches up later
        end
        self.annotation_close_sync_in_flight = true
        Trapper:wrap(function()
            local run_ok, result, err = pcall(function()
                if not captured.hash then
                    captured.hash = BridgeAnnotations.resolveBookHash(captured.file)
                end
                if not captured.hash then
                    return nil, "no hash"
                end
                return BridgeAnnotations.exchangeBooks(self, { captured })
            end)
            self.annotation_close_sync_in_flight = false
            if run_ok and type(result) == "table" then
                self:logInfo("Close-sync highlights:", tostring(result.uploaded), "uploaded,",
                    tostring(result.applied), "applied,", tostring(result.deleted), "deleted")
            elseif run_ok and err and err ~= "no hash" then
                self:logWarn("Close-sync highlights failed:", tostring(err))
            elseif not run_ok then
                self:logWarn("Close-sync highlights crashed:", tostring(result))
            end
        end)
    end)
end

function BridgeSync:onSuspend()
    self:endSession({ silent = true, force_queue = true })
    return false
end

function BridgeSync:onBridgeSyncSyncBooks()
    Trapper:wrap(function()
        self:syncFromBridge(false)
    end)
    return true
end

function BridgeSync:onBridgeSyncSyncStats()
    Trapper:wrap(function()
        self:syncReadingStats(false)
    end)
    return true
end

function BridgeSync:checkForPluginUpdate()
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
        text = _("Checking for plugin update..."),
        timeout = 0,
    }
    UIManager:show(info_msg)
    UIManager:forceRePaint()

    local subprocess_ok, ok, result = self:_runInSubprocess(function()
        return self.api:getPluginVersion()
    end)

    UIManager:close(info_msg)

    if not subprocess_ok then
        self:logErr("Plugin version check subprocess failed", ok or "")
        self:_showMessage(T(_("Plugin version check failed: %1"), tostring(ok or "Subprocess failed")), 5)
        return
    end

    if not ok then
        self:logWarn(result or "Version check failed")
        self:_showMessage(result or _("Version check failed"), 4)
        return
    end

    local remote_version = type(result) == "table" and result.version or nil
    if not remote_version then
        self:_showMessage(_("Invalid version response from server"), 4)
        return
    end

    local local_version = "unknown"
    local chunk = loadfile(self.path .. "/_meta.lua")
    if chunk then
        local meta_ok, meta_table = pcall(chunk)
        if meta_ok and type(meta_table) == "table" and meta_table.version then
            local_version = meta_table.version
        end
    end

    if local_version == remote_version then
        self:_showMessage(T(_("Plugin is up to date (v%1)"), local_version), 3)
        return
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
end

-- Wraps a path in POSIX single quotes, escaping embedded single quotes.
local function _shellQuote(path)
    return "'" .. tostring(path):gsub("'", "'\\''") .. "'"
end

function BridgeSync:_downloadAndInstallPlugin(version)
    local temp_path = DataStorage:getSettingsDir() .. "/bridgesync-update.zip"

    local info_msg = InfoMessage:new{
        text = T(_("Downloading plugin v%1..."), version),
        timeout = 0,
    }
    UIManager:show(info_msg)
    UIManager:forceRePaint()

    local subprocess_ok, ok, err = self:_runInSubprocess(function()
        return self.api:downloadPluginZip(temp_path)
    end)

    UIManager:close(info_msg)

    if not subprocess_ok then
        self:logErr("Plugin download subprocess failed", ok or "")
        self:_showMessage(T(_("Plugin download failed: %1"), tostring(ok or "Subprocess failed")), 5)
        return
    end

    if not ok then
        self:logWarn(err or "Download failed")
        self:_showMessage(err or _("Plugin download failed"), 4)
        return
    end

    -- Atomic install: extract into a staging directory (a partial unpack never
    -- touches the live plugin), back up the current plugin dir, rename the new
    -- one into place, and restore the backup on any failure. Uses KOReader's
    -- archive helper — platform `unzip` exit codes vary and some devices don't
    -- ship unzip at all. Staging/backup live next to the plugin dir so the
    -- renames stay on one filesystem.
    local install_ok, install_err = self:_installPluginZip(temp_path)
    os.remove(temp_path)

    if not install_ok then
        self:logErr("Plugin update failed:", tostring(install_err))
        self:_showMessage(T(_("Plugin update failed: %1"), tostring(install_err)), 6)
        return
    end

    self:_showMessage(T(_("Plugin updated to v%1. Please restart KOReader."), version), 8)
end

function BridgeSync:_installPluginZip(zip_path)
    local plugin_dir = tostring(self.path):gsub("/+$", "")
    local plugins_dir = plugin_dir:match("^(.+)/[^/]+$")
    local plugin_name = plugin_dir:match("([^/]+)$")
    if not plugins_dir or not plugin_name then
        return nil, "cannot determine plugin directory layout"
    end

    local staging = plugins_dir .. "/" .. plugin_name .. ".update"
    local backup = plugins_dir .. "/" .. plugin_name .. ".bak"

    -- Clear any leftovers from a previously interrupted attempt.
    os.execute("rm -rf " .. _shellQuote(staging))

    if not lfs.mkdir(staging) and lfs.attributes(staging, "mode") ~= "directory" then
        return nil, "could not create staging directory"
    end

    local Device = require("device")
    local unpack_ok, unpack_err
    if type(Device.unpackArchive) == "function" then
        unpack_ok, unpack_err = Device:unpackArchive(zip_path, staging, true)
    else
        -- Very old KOReader without the helper: fall back to unzip, but still
        -- into staging only.
        local exit_code = os.execute(
            "unzip -o " .. _shellQuote(zip_path) .. " -d " .. _shellQuote(staging) .. " >/dev/null 2>&1"
        )
        unpack_ok = (exit_code == 0 or exit_code == true)
        unpack_err = unpack_ok and nil or "unzip failed"
    end
    if not unpack_ok then
        os.execute("rm -rf " .. _shellQuote(staging))
        return nil, tostring(unpack_err or "archive extraction failed")
    end

    -- The zip carries a bridgesync.koplugin/ top-level folder; sanity-check it.
    local staged_plugin = staging .. "/" .. plugin_name
    if lfs.attributes(staged_plugin .. "/_meta.lua", "mode") ~= "file" then
        os.execute("rm -rf " .. _shellQuote(staging))
        return nil, "downloaded archive is missing " .. plugin_name .. "/_meta.lua"
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
                    Trapper:wrap(function()
                        self:syncFromBridge(false)
                    end)
                end,
            },
            {
                text = _("Sync Reading Stats"),
                callback = function()
                    Trapper:wrap(function()
                        self:syncReadingStats(false)
                    end)
                end,
            },
            {
                text = _("Sync Highlights"),
                callback = function()
                    Trapper:wrap(function()
                        self:syncAnnotations(false)
                    end)
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
                        self:_uploadSessions()
                        self:_showMessage(T(_("Uploaded %1 session(s)"), #self.pending_sessions), 2)
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
                text = _("Check for Plugin Update"),
                callback = function()
                    Trapper:wrap(function()
                        self:checkForPluginUpdate()
                    end)
                end,
            },
        },
    }
end

return BridgeSync
