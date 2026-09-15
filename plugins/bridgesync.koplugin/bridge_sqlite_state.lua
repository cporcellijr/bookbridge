-- SQLite-backed state for the BridgeSync plugin (device-local bridge_sync.db).
-- Optional backend: when KOReader's lua-ljsqlite3 is unavailable the plugin
-- falls back to LuaSettings flat files (see main.lua).
--
-- lua-ljsqlite3 API contract this module is written against:
--   * stmt:bind(...) binds ALL varargs positionally from parameter 1;
--     binding a single parameter by index is stmt:bind1(i, v).
--   * stmt:step() returns a row table for each SELECT row, returns nil on
--     completion (SQLITE_DONE) -- including after a successful INSERT or
--     UPDATE -- and RAISES a Lua error on real failures.
--   * Statements are released with stmt:close(); there is no finalize().
--   * INTEGER columns come back as int64 cdata; convert with tonumber().
--   * conn:exec() splits its input on ";" -- never put a ";" inside a
--     statement body in SCHEMA_SQL / INDEX_SQL.

local SQ3
do
    local ok, mod = pcall(require, "lua-ljsqlite3/init")
    if ok then
        SQ3 = mod
    end
end

local DataStorage = require("datastorage")
local logger = require("logger")
local json = require("json")

-- KOReader runs LuaJIT (global unpack); the test harness may run Lua 5.2+.
local unpack = unpack or table.unpack

local PLUGIN_NAME = "bridgesync"

-- Wait this long for a competing writer before surfacing SQLITE_BUSY.
local BUSY_TIMEOUT_MS = 5000

-- State-file keys that intentionally stay in LuaSettings:
--   annotation_watermarks -- bridge_annotations.lua still reads/writes
--   bridge.state directly, so migrating it would strand live data.
local MIGRATION_SKIP_STATE_KEYS = {
    items = true,
    pending_sessions = true,
    annotation_watermarks = true,
}

local SCHEMA_SQL = [[
CREATE TABLE IF NOT EXISTS plugin_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_name TEXT NOT NULL,
    setting_key TEXT NOT NULL,
    setting_value TEXT,
    setting_type TEXT NOT NULL DEFAULT 'string',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plugin_name, setting_key)
);
CREATE TABLE IF NOT EXISTS plugin_state_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_name TEXT NOT NULL,
    abs_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    item_value TEXT,
    item_type TEXT NOT NULL DEFAULT 'string',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plugin_name, abs_id, item_key)
);
CREATE TABLE IF NOT EXISTS plugin_sync_timestamps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_name TEXT NOT NULL,
    abs_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    sync_type TEXT NOT NULL,
    last_synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sync_hash TEXT,
    UNIQUE(plugin_name, abs_id, service_name, sync_type)
);
CREATE TABLE IF NOT EXISTS plugin_pending_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    abs_id TEXT,
    document_hash TEXT,
    session_type TEXT,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    start_page INTEGER,
    end_page INTEGER,
    start_progress REAL,
    end_progress REAL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plugin_name, session_id)
)
]]

local INDEX_SQL = [[
CREATE INDEX IF NOT EXISTS idx_state_items_abs ON plugin_state_items(plugin_name, abs_id);
CREATE INDEX IF NOT EXISTS idx_settings_key ON plugin_settings(plugin_name, setting_key);
CREATE INDEX IF NOT EXISTS idx_sync_timestamps_key ON plugin_sync_timestamps(plugin_name, abs_id, service_name, sync_type);
CREATE INDEX IF NOT EXISTS idx_pending_sessions_abs ON plugin_pending_sessions(plugin_name, abs_id, uploaded)
]]

-- Named SQL statements. tests/test_bridgesync_sqlite_state.py extracts these
-- (and the schema above) straight from this file and runs them against real
-- SQLite, so behavior coverage cannot drift from what ships.
local SQL = {
    get_setting = [[
SELECT setting_value, setting_type FROM plugin_settings
WHERE plugin_name = ? AND setting_key = ?
]],
    set_setting = [[
INSERT OR REPLACE INTO plugin_settings
(plugin_name, setting_key, setting_value, setting_type, updated_at)
VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
]],
    delete_setting = [[
DELETE FROM plugin_settings WHERE plugin_name = ? AND setting_key = ?
]],
    get_state_item = [[
SELECT item_value, item_type FROM plugin_state_items
WHERE plugin_name = ? AND abs_id = ? AND item_key = ?
]],
    set_state_item = [[
INSERT OR REPLACE INTO plugin_state_items
(plugin_name, abs_id, item_key, item_value, item_type, updated_at)
VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
]],
    get_state_items_for_book = [[
SELECT item_key, item_value, item_type FROM plugin_state_items
WHERE plugin_name = ? AND abs_id = ?
]],
    get_state_books = [[
SELECT DISTINCT abs_id FROM plugin_state_items
WHERE plugin_name = ? ORDER BY abs_id
]],
    delete_all_state_items = [[
DELETE FROM plugin_state_items WHERE plugin_name = ?
]],
    get_sync_timestamp = [[
SELECT last_synced_at, sync_hash FROM plugin_sync_timestamps
WHERE plugin_name = ? AND abs_id = ? AND service_name = ? AND sync_type = ?
]],
    set_sync_timestamp = [[
INSERT OR REPLACE INTO plugin_sync_timestamps
(plugin_name, abs_id, service_name, sync_type, last_synced_at, sync_hash)
VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
]],
    add_pending_session = [[
INSERT INTO plugin_pending_sessions
(plugin_name, session_id, abs_id, document_hash, session_type,
 start_time, end_time, duration_seconds, start_page, end_page,
 start_progress, end_progress, uploaded)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
]],
    select_sessions = [[
SELECT session_id, abs_id, document_hash, session_type,
       start_time, end_time, duration_seconds, start_page, end_page,
       start_progress, end_progress, uploaded
FROM plugin_pending_sessions
WHERE plugin_name = ?
]],
    find_mergeable_session = [[
SELECT session_id, abs_id, document_hash, session_type,
       start_time, end_time, duration_seconds, start_page, end_page,
       start_progress, end_progress, uploaded
FROM plugin_pending_sessions
WHERE plugin_name = ? AND abs_id = ? AND uploaded = 0
  AND (? - end_time) BETWEEN 0 AND ?
ORDER BY end_time DESC LIMIT 1
]],
    merge_session_end = [[
UPDATE plugin_pending_sessions
SET end_time = ?, end_page = ?, end_progress = ?,
    duration_seconds = duration_seconds + ?
WHERE plugin_name = ? AND session_id = ? AND uploaded = 0
]],
    prune_uploaded_sessions = [[
DELETE FROM plugin_pending_sessions
WHERE plugin_name = ? AND uploaded = 1 AND end_time < ?
]],
}

local BridgeSqliteState = {}

local function get_database_path()
    return DataStorage:getSettingsDir() .. "/bridge_sync.db"
end

-- Encode a Lua value into (text, type tag) for storage.
local function encode_value(value)
    local value_type = type(value)
    if value_type == "table" then
        return json.encode(value), "table"
    end
    return tostring(value), value_type
end

-- Decode a stored (text, type tag) pair; `default` on missing/undecodable.
local function decode_value(value_str, value_type, default)
    if value_str == nil then
        return default
    end
    if value_type == "number" then
        local n = tonumber(value_str)
        if n == nil then return default end
        return n
    elseif value_type == "boolean" then
        if value_str == "true" then return true end
        if value_str == "false" then return false end
        return default
    elseif value_type == "table" then
        local ok, decoded = pcall(json.decode, value_str)
        if ok then return decoded end
        return default
    end
    return value_str
end

local function row_to_session(row)
    return {
        session_id = row[1],
        abs_id = row[2],
        document_hash = row[3],
        session_type = row[4],
        start_time = tonumber(row[5]),
        end_time = tonumber(row[6]),
        duration_seconds = tonumber(row[7]) or 0,
        start_page = tonumber(row[8]),
        end_page = tonumber(row[9]),
        start_progress = tonumber(row[10]),
        end_progress = tonumber(row[11]),
        uploaded = tonumber(row[12]) == 1,
    }
end

local function new_session_id()
    return tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
end

function BridgeSqliteState:new()
    local instance = {}
    setmetatable(instance, { __index = BridgeSqliteState })
    return instance
end

function BridgeSqliteState:is_available()
    return SQ3 ~= nil
end

function BridgeSqliteState:init()
    if not SQ3 then
        return false, "SQLite not available"
    end

    local ok, conn = pcall(SQ3.open, get_database_path())
    if not ok then
        logger.err("BridgeSqliteState: failed to open database:", tostring(conn))
        return false, conn
    end
    self.conn = conn

    -- Another writer (a second plugin instance, or a still-live forked
    -- child holding an inherited handle) must make this connection wait
    -- rather than fail the whole operation with SQLITE_BUSY.
    local busy_ok, busy_err = pcall(function()
        conn:set_busy_timeout(BUSY_TIMEOUT_MS)
    end)
    if not busy_ok then
        logger.warn("BridgeSqliteState: failed to set busy timeout:", tostring(busy_err))
    end

    local schema_ok, err = pcall(function()
        conn:exec(SCHEMA_SQL)
        conn:exec(INDEX_SQL)
    end)
    if not schema_ok then
        logger.err("BridgeSqliteState: failed to create schema:", tostring(err))
        pcall(function() conn:close() end)
        self.conn = nil
        return false, err
    end

    return true
end

function BridgeSqliteState:close()
    if self.conn then
        pcall(function() self.conn:close() end)
        self.conn = nil
    end
end

-- Prepare + bind + drain a statement. Raises on failure (lua-ljsqlite3
-- error convention); wrap with _run/_rows for the pcall'd public surface.
-- Returns the collected rows (empty table for writes).
function BridgeSqliteState:_execute(sql_text, ...)
    local stmt = self.conn:prepare(sql_text)
    local n = select("#", ...)
    for i = 1, n do
        local value = select(i, ...)
        if value ~= nil then
            stmt:bind1(i, value)
        end
    end
    local rows = {}
    local row = stmt:step()
    while row do
        table.insert(rows, row)
        row = stmt:step()
    end
    stmt:close()
    return rows
end

-- Run a write statement. Returns true on success, false (logged) on failure.
function BridgeSqliteState:_run(op, sql_text, ...)
    if not self.conn then return false end
    local ok, err = pcall(self._execute, self, sql_text, ...)
    if not ok then
        logger.warn("BridgeSqliteState:" .. op .. " failed:", tostring(err))
        return false
    end
    return true
end

-- Run a read statement. Returns the row list, or nil (logged) on failure.
function BridgeSqliteState:_rows(op, sql_text, ...)
    if not self.conn then return nil end
    local ok, rows = pcall(self._execute, self, sql_text, ...)
    if not ok then
        logger.warn("BridgeSqliteState:" .. op .. " failed:", tostring(rows))
        return nil
    end
    return rows
end

-- Settings ------------------------------------------------------------------

function BridgeSqliteState:get_setting(key, default)
    local rows = self:_rows("get_setting", SQL.get_setting, PLUGIN_NAME, key)
    if not rows or not rows[1] then return default end
    return decode_value(rows[1][1], rows[1][2], default)
end

function BridgeSqliteState:set_setting(key, value)
    if value == nil then
        return self:_run("delete_setting", SQL.delete_setting, PLUGIN_NAME, key)
    end
    local value_str, value_type = encode_value(value)
    return self:_run("set_setting", SQL.set_setting,
        PLUGIN_NAME, key, value_str, value_type)
end

-- Per-book state items ------------------------------------------------------

function BridgeSqliteState:get_state_item(abs_id, key, default)
    local rows = self:_rows("get_state_item", SQL.get_state_item,
        PLUGIN_NAME, abs_id, key)
    if not rows or not rows[1] then return default end
    return decode_value(rows[1][1], rows[1][2], default)
end

function BridgeSqliteState:get_all_state_items_for_book(abs_id)
    local rows = self:_rows("get_all_state_items_for_book",
        SQL.get_state_items_for_book, PLUGIN_NAME, abs_id)
    local items = {}
    for _, row in ipairs(rows or {}) do
        items[row[1]] = decode_value(row[2], row[3], nil)
    end
    return items
end

function BridgeSqliteState:get_all_books()
    local rows = self:_rows("get_all_books", SQL.get_state_books, PLUGIN_NAME)
    local books = {}
    for _, row in ipairs(rows or {}) do
        table.insert(books, row[1])
    end
    return books
end

-- Atomically replace the whole item map (mirrors the LuaSettings backend's
-- full-replace of the "items" table) and store the manifest revision.
function BridgeSqliteState:replace_state(items, revision)
    if not self.conn then return false end
    local ok, err = pcall(function()
        self.conn:exec("BEGIN TRANSACTION")
        self:_execute(SQL.delete_all_state_items, PLUGIN_NAME)
        for abs_id, entry in pairs(items or {}) do
            if type(entry) == "table" then
                for key, value in pairs(entry) do
                    local value_str, value_type = encode_value(value)
                    self:_execute(SQL.set_state_item,
                        PLUGIN_NAME, abs_id, key, value_str, value_type)
                end
            end
        end
        if revision ~= nil then
            local value_str, value_type = encode_value(revision)
            self:_execute(SQL.set_setting,
                PLUGIN_NAME, "revision", value_str, value_type)
        end
        self.conn:exec("COMMIT")
    end)
    if not ok then
        pcall(function() self.conn:exec("ROLLBACK") end)
        logger.warn("BridgeSqliteState:replace_state failed:", tostring(err))
        return false
    end
    return true
end

-- Sync fingerprints ---------------------------------------------------------

function BridgeSqliteState:get_sync_timestamp(abs_id, service_name, sync_type)
    local rows = self:_rows("get_sync_timestamp", SQL.get_sync_timestamp,
        PLUGIN_NAME, abs_id, service_name, sync_type)
    if not rows or not rows[1] then return nil end
    return {
        last_synced_at = rows[1][1],
        sync_hash = rows[1][2],
    }
end

function BridgeSqliteState:set_sync_timestamp(abs_id, service_name, sync_type, sync_hash)
    return self:_run("set_sync_timestamp", SQL.set_sync_timestamp,
        PLUGIN_NAME, abs_id, service_name, sync_type, sync_hash or "")
end

-- Pending reading sessions --------------------------------------------------

-- Insert a session row. Persists every field the bridge's session-upload
-- endpoint consumes (abs_id OR document_hash for book resolution,
-- session_type, duration_seconds, times, progress) so sessions survive a
-- device restart intact. Returns the session_id, or nil on failure.
function BridgeSqliteState:add_pending_session(session)
    if not self.conn then return nil end
    local start_time = tonumber(session.start_time)
    local end_time = tonumber(session.end_time)
    if not start_time or not end_time then
        logger.warn("BridgeSqliteState:add_pending_session skipped - non-numeric times")
        return nil
    end
    local session_id = session.session_id or new_session_id()
    local ok = self:_run("add_pending_session", SQL.add_pending_session,
        PLUGIN_NAME,
        session_id,
        session.abs_id,
        session.document_hash,
        session.session_type,
        math.floor(start_time),
        math.floor(end_time),
        math.floor(tonumber(session.duration_seconds) or 0),
        tonumber(session.start_page),
        tonumber(session.end_page),
        tonumber(session.start_progress),
        tonumber(session.end_progress))
    if not ok then return nil end
    return session_id
end

-- List sessions in upload-payload shape, oldest first.
-- `abs_id` and `uploaded` are optional filters.
function BridgeSqliteState:get_pending_sessions(abs_id, uploaded)
    local sql_text = SQL.select_sessions
    local params = { PLUGIN_NAME }
    if abs_id ~= nil then
        sql_text = sql_text .. " AND abs_id = ?"
        table.insert(params, abs_id)
    end
    if uploaded ~= nil then
        sql_text = sql_text .. " AND uploaded = ?"
        table.insert(params, uploaded and 1 or 0)
    end
    sql_text = sql_text .. " ORDER BY start_time"
    local rows = self:_rows("get_pending_sessions", sql_text, unpack(params))
    local sessions = {}
    for _, row in ipairs(rows or {}) do
        table.insert(sessions, row_to_session(row))
    end
    return sessions
end

-- Newest un-uploaded session for `abs_id` whose end_time is within
-- `threshold_seconds` before `start_time`, or nil.
function BridgeSqliteState:find_mergeable_pending_session(abs_id, start_time, threshold_seconds)
    if not abs_id then return nil end
    local start_num = tonumber(start_time)
    if not start_num then return nil end
    local rows = self:_rows("find_mergeable_pending_session",
        SQL.find_mergeable_session,
        PLUGIN_NAME, abs_id, math.floor(start_num), threshold_seconds or 300)
    if not rows or not rows[1] then return nil end
    return row_to_session(rows[1])
end

-- Extend a session's end state and accumulate its reading duration
-- (mirrors BridgeSessions.applyMerge).
function BridgeSqliteState:merge_pending_session_end(session_id, end_time, end_page, end_progress, add_duration)
    local end_num = tonumber(end_time)
    if not end_num then return false end
    return self:_run("merge_pending_session_end", SQL.merge_session_end,
        math.floor(end_num),
        tonumber(end_page),
        tonumber(end_progress),
        math.floor(tonumber(add_duration) or 0),
        PLUGIN_NAME,
        session_id)
end

-- Flag a batch of sessions as uploaded (chunked to stay clear of SQLite's
-- bound-parameter limit).
function BridgeSqliteState:mark_sessions_uploaded(session_ids)
    if not session_ids or #session_ids == 0 then return false end
    local all_ok = true
    local chunk_size = 200
    for offset = 1, #session_ids, chunk_size do
        local last = math.min(offset + chunk_size - 1, #session_ids)
        local placeholders = {}
        local params = { PLUGIN_NAME }
        for i = offset, last do
            table.insert(placeholders, "?")
            table.insert(params, session_ids[i])
        end
        local sql_text = "UPDATE plugin_pending_sessions"
            .. " SET uploaded = 1 WHERE plugin_name = ? AND session_id IN ("
            .. table.concat(placeholders, ", ") .. ")"
        if not self:_run("mark_sessions_uploaded", sql_text, unpack(params)) then
            all_ok = false
        end
    end
    return all_ok
end

-- Drop uploaded rows older than `before_epoch` so the table stays bounded.
function BridgeSqliteState:prune_uploaded_sessions(before_epoch)
    return self:_run("prune_uploaded_sessions", SQL.prune_uploaded_sessions,
        PLUGIN_NAME, math.floor(tonumber(before_epoch) or 0))
end

-- Migration -----------------------------------------------------------------

-- One-time import of both LuaSettings files. Copies: every settings-file
-- key; the state file's per-book "items" map; every state-file scalar the
-- plugin reads through _getStateScalar/_getStateScalarJSON (revision, stats
-- watermarks, last_sync_job, hash_cache, plugin-update bookkeeping); and the
-- pending_sessions backlog. The source files are left untouched.
function BridgeSqliteState:migrate_from_luasettings(luasettings_state, luasettings_settings)
    if not self.conn then
        return false, "Database not initialized"
    end

    local ok, err = pcall(function()
        self.conn:exec("BEGIN TRANSACTION")

        if luasettings_settings and type(luasettings_settings.data) == "table" then
            for key, value in pairs(luasettings_settings.data) do
                local value_str, value_type = encode_value(value)
                self:_execute(SQL.set_setting, PLUGIN_NAME, key, value_str, value_type)
            end
        end

        if luasettings_state and type(luasettings_state.data) == "table" then
            for key, value in pairs(luasettings_state.data) do
                if not MIGRATION_SKIP_STATE_KEYS[key] then
                    if type(value) == "table" then
                        -- Matches _getStateScalarJSON, which expects a JSON
                        -- string stored under the plain settings channel.
                        self:_execute(SQL.set_setting,
                            PLUGIN_NAME, key, json.encode(value), "string")
                    else
                        local value_str, value_type = encode_value(value)
                        self:_execute(SQL.set_setting,
                            PLUGIN_NAME, key, value_str, value_type)
                    end
                end
            end

            local items = luasettings_state.data.items or {}
            for abs_id, entry in pairs(items) do
                if type(entry) == "table" then
                    for key, value in pairs(entry) do
                        local value_str, value_type = encode_value(value)
                        self:_execute(SQL.set_state_item,
                            PLUGIN_NAME, abs_id, key, value_str, value_type)
                    end
                end
            end

            for _, session in ipairs(luasettings_state.data.pending_sessions or {}) do
                local start_time = tonumber(session.start_time)
                local end_time = tonumber(session.end_time)
                if start_time and end_time then
                    self:_execute(SQL.add_pending_session,
                        PLUGIN_NAME,
                        session.session_id or new_session_id(),
                        session.abs_id,
                        session.document_hash,
                        session.session_type,
                        math.floor(start_time),
                        math.floor(end_time),
                        math.floor(tonumber(session.duration_seconds) or 0),
                        tonumber(session.start_page),
                        tonumber(session.end_page),
                        tonumber(session.start_progress),
                        tonumber(session.end_progress))
                end
            end
        end

        self.conn:exec("COMMIT")
    end)

    if not ok then
        pcall(function() self.conn:exec("ROLLBACK") end)
        logger.err("BridgeSqliteState: migration failed:", tostring(err))
        return false, err
    end

    return true
end

return BridgeSqliteState
