local DataStorage = require("datastorage")
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
local APIClient = require("bridge_api_client")
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

function BridgeSync:init()
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
    self.current_session = nil
    self.pending_sessions = self.state:readSetting("pending_sessions") or {}

    self.sync_in_progress = false
    self.last_auto_sync_time = 0
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
    self.settings:flush()
    self.api:init(self.server_url, self.username, self.key, function(level, message)
        self:_appendLog(level, message)
    end)
end

function BridgeSync:_extractHost()
    return tostring(self.server_url or ""):match("^https?://([^/%:]+)")
end

function BridgeSync:_preflightNetwork()
    if not NetworkMgr:isConnected() then
        return false, _("WiFi is not connected")
    end

    local host = self:_extractHost()
    if not host or host == "" then
        return false, _("Server URL is invalid")
    end

    local resolved_ip = socket.dns.toip(host)
    if not resolved_ip then
        return false, T(_("DNS lookup failed for %1"), host)
    end

    self:logInfo("Resolved host", host, "to", resolved_ip)
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

function BridgeSync:_showManagedFolderChooser()
    local start_path = self.download_dir
    if lfs.attributes(start_path, "mode") ~= "directory" then
        start_path = self:_detectDefaultDownloadDir()
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
                self:_showMessage(T(_("Managed Folder set to %1"), self.download_dir), 3)
            end,
        }
        UIManager:show(chooser)
        return
    end

    self:_promptForSetting(
        _("Managed Folder"),
        self.download_dir,
        _("Enter managed folder path"),
        function(value)
            local selected = self:_normalizePath(value)
            if selected == "" then
                return
            end
            self.download_dir = selected
            self:_saveSettings()
        end
    )
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

function BridgeSync:_promptForSetting(title, current_value, hint, setter, is_password)
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

function BridgeSync:_scheduleSync(delay_seconds, silent)
    if self.sync_scheduled then
        return
    end

    self.sync_scheduled = true
    UIManager:scheduleIn(delay_seconds or 10, function()
        self.sync_scheduled = false
        if not self.is_enabled or not NetworkMgr:isConnected() then
            return
        end
        if self:_shouldAvoidAutoSyncWhileReading() then
            self.needs_wake_sync = true
            self:logInfo("Deferring auto-sync while a document is open")
            return
        end
        self.needs_wake_sync = false
        self.last_auto_sync_time = os.time()
        Trapper:wrap(function()
            self:syncFromBridge(silent == nil and true or silent)
        end)
    end)
end

function BridgeSync:onResume()
    if not self.is_enabled then
        return false
    end

    -- Restart session tracking if a book is open
    if self.session_tracking_enabled and self.ui and self.ui.document then
        self:startSession()
    end

    -- Upload any queued sessions
    if #self.pending_sessions > 0 and NetworkMgr:isConnected() then
        self:_uploadSessions()
    end

    if self.manual_only then
        return false
    end
    if not self.auto_sync_on_resume or self:_isCooldownActive() then
        return false
    end

    self.needs_wake_sync = true
    if NetworkMgr:isConnected() then
        self:_scheduleSync(self.wake_sync_delay_seconds, true)
    end
    return false
end

function BridgeSync:onNetworkConnected()
    if not self.is_enabled then
        return false
    end
    if self.manual_only then
        return false
    end

    if self.needs_wake_sync and not self:_isCooldownActive() then
        self.needs_wake_sync = false
        if self:_shouldAvoidAutoSyncWhileReading() then
            self.needs_wake_sync = true
            return false
        end
        self:_scheduleSync(self.wake_sync_delay_seconds, true)
        return false
    end

    if self.auto_sync_on_network and not self:_isCooldownActive() then
        if self:_shouldAvoidAutoSyncWhileReading() then
            self.needs_wake_sync = true
            return false
        end
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

    for entry in lfs.dir(self.download_dir) do
        if entry ~= "." and entry ~= ".." and not entry:match("%.part$") then
            local path = self.download_dir .. "/" .. entry
            if lfs.attributes(path, "mode") == "file" then
                local hash = self:_calculateBookHash(path)
                if hash and not index[hash] then
                    index[hash] = path
                end
            end
        end
    end
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
    local hash_index = self:_buildHashIndex()
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
            local indexed_path = hash_index[book.content_hash]
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
                }
                hash_index[book.content_hash] = target_path
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
                        }
                        hash_index[book.content_hash] = target_path
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

    local network_ok, network_err = self:_preflightNetwork()
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

function BridgeSync:onReaderReady()
    self:startSession()
    return false
end

function BridgeSync:onCloseDocument()
    self:endSession({ force_queue = false })
    return false
end

function BridgeSync:onSuspend()
    self:endSession({ silent = true, force_queue = true })
    return false
end

function BridgeSync:addToMainMenu(menu_items)
    menu_items.bridge_sync = {
        text = _("Bridge Sync"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("Enable Sync"),
                checked_func = function()
                    return self.is_enabled
                end,
                callback = function()
                    self.is_enabled = not self.is_enabled
                    self:_saveSettings()
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
                text = _("Manual Only"),
                checked_func = function()
                    return self.manual_only
                end,
                callback = function()
                    self.manual_only = not self.manual_only
                    self:_saveSettings()
                end,
            },
            {
                text = _("Auto-Sync on Wake"),
                checked_func = function()
                    return self.auto_sync_on_resume
                end,
                callback = function()
                    self.auto_sync_on_resume = not self.auto_sync_on_resume
                    self:_saveSettings()
                end,
            },
            {
                text = _("Auto-Sync on Network"),
                checked_func = function()
                    return self.auto_sync_on_network
                end,
                callback = function()
                    self.auto_sync_on_network = not self.auto_sync_on_network
                    self:_saveSettings()
                end,
            },
            {
                text = _("Do Not Sync While Reading"),
                checked_func = function()
                    return self.do_not_sync_while_book_open
                end,
                callback = function()
                    self.do_not_sync_while_book_open = not self.do_not_sync_while_book_open
                    self:_saveSettings()
                end,
            },
            {
                text = _("Delete Removed Books"),
                checked_func = function()
                    return self.delete_removed_books
                end,
                callback = function()
                    self.delete_removed_books = not self.delete_removed_books
                    self:_saveSettings()
                end,
            },
            {
                text = _("Track Reading Sessions"),
                checked_func = function()
                    return self.session_tracking_enabled
                end,
                callback = function()
                    self.session_tracking_enabled = not self.session_tracking_enabled
                    self:_saveSettings()
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
                callback = function()
                    self:_promptForSetting(
                        _("Bridge Server URL"),
                        self.server_url,
                        _("Enter bridge base URL"),
                        function(value)
                            self.server_url = value
                            self:_saveSettings()
                        end
                    )
                end,
            },
            {
                text_func = function()
                    return T(_("Username: %1"), self.username ~= "" and self.username or _("Not set"))
                end,
                callback = function()
                    self:_promptForSetting(
                        _("Bridge Username"),
                        self.username,
                        _("Enter KOSync username"),
                        function(value)
                            self.username = value
                            self:_saveSettings()
                        end
                    )
                end,
            },
            {
                text = _("Configure Key"),
                callback = function()
                    self:_promptForSetting(
                        _("Bridge Key"),
                        self.key,
                        _("Enter KOSync key"),
                        function(value)
                            self.key = value
                            self:_saveSettings()
                        end,
                        true
                    )
                end,
            },
            {
                text_func = function()
                    return T(_("Wake Sync Delay: %1s"), self.wake_sync_delay_seconds)
                end,
                callback = function()
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
                        end
                    )
                end,
            },
            {
                text_func = function()
                    return T(_("Managed Folder: %1"), self.download_dir)
                end,
                callback = function()
                    self:_showManagedFolderChooser()
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
        },
    }
end

return BridgeSync
