local plugin_dir = assert(arg[1], "plugin directory argument required")
package.path = plugin_dir .. "/?.lua;" .. package.path

local fake_docsettings = {
    hasSidecarFile = function() return false end,
    open = function() return { readSetting = function() return {} end } end,
}
local fake_lfs = { attributes = function() return nil end }
package.preload["docsettings"] = function() return fake_docsettings end
package.preload["libs/libkoreader-lfs"] = function() return fake_lfs end
package.preload["logger"] = function()
    return { info = function() end, warn = function() end, err = function() end }
end
package.preload["ffi/sha2"] = function()
    return { md5 = function(value) return tostring(value):sub(1, 32) end }
end
package.preload["datastorage"] = function()
    return { getSettingsDir = function() return "/tmp/bridgesync-test" end }
end
-- bridge_sqlite_state only touches json for table-typed values; the contract
-- tests below use scalars only, so hitting json here means a test regressed.
package.preload["json"] = function()
    return {
        encode = function() error("json.encode must not be reached by scalar tests") end,
        decode = function() error("json.decode must not be reached by scalar tests") end,
    }
end
package.preload["socket"] = function()
    return {
        skip = function(count, ...)
            return select(count + 1, ...)
        end,
        sleep = function() end,
    }
end
local fake_http_body = "ok"
local fake_http_content_length
local fake_plugin_digest = string.rep("d", 64)
package.preload["socket.http"] = function()
    return {
        request = function(request)
            request.sink(fake_http_body)
            request.sink(nil)
            return 1, 200, {
                ["content-length"] = tostring(fake_http_content_length or #fake_http_body),
                ["x-content-sha256"] = fake_plugin_digest,
            }, "OK"
        end,
    }
end
package.preload["ltn12"] = function()
    return { source = { string = function(value) return value end } }
end
package.preload["socketutil"] = function()
    return {
        TIMEOUT_CODE = -1,
        SSL_HANDSHAKE_CODE = -2,
        SINK_TIMEOUT_CODE = -3,
        set_timeout = function() end,
        reset_timeout = function() end,
        table_sink = function(target)
            return function(chunk)
                if chunk then target[#target + 1] = chunk end
                return 1
            end
        end,
        file_sink = function(handle)
            return function(chunk)
                if chunk then
                    handle:write(chunk)
                else
                    handle:close()
                end
                return 1
            end
        end,
    }
end
local scheduled_callbacks = {}
package.preload["ui/uimanager"] = function()
    return {
        scheduleIn = function(_, delay_or_callback, callback)
            scheduled_callbacks[#scheduled_callbacks + 1] = callback or delay_or_callback
        end,
    }
end
package.preload["ui/trapper"] = function()
    return { wrap = function(_, callback) callback() end }
end
local fake_history = {}
package.preload["readhistory"] = function() return { hist = fake_history } end

-- Faithful fake of KOReader's lua-ljsqlite3 statement/connection API:
--   * bind(...) binds all varargs positionally; bind1(i, v) binds one index
--   * step() pops scripted rows and returns nil when drained (SQLITE_DONE)
--   * statements are released with close(); there is no finalize()
local fake_sq3
do
    local Stmt = {}
    Stmt.__index = Stmt
    function Stmt:bind1(i, v)
        self.params[i] = v
        return self
    end
    function Stmt:bind(...)
        for i = 1, select("#", ...) do
            self:bind1(i, (select(i, ...)))
        end
        return self
    end
    function Stmt:step()
        return table.remove(self.rows, 1)
    end
    function Stmt:reset()
        return self
    end
    function Stmt:close()
        self.closed = true
    end

    local Conn = {}
    Conn.__index = Conn
    function Conn:prepare(sql)
        local stmt = setmetatable({
            sql = sql,
            params = {},
            rows = table.remove(self.scripted_rows, 1) or {},
            closed = false,
        }, Stmt)
        table.insert(self.prepared, stmt)
        return stmt
    end
    function Conn:exec(sql)
        table.insert(self.executed, sql)
    end
    function Conn:close()
        self.conn_closed = true
    end
    function Conn:script_rows(rows)
        table.insert(self.scripted_rows, rows)
    end

    fake_sq3 = {
        new_conn = function()
            return setmetatable({
                prepared = {},
                executed = {},
                scripted_rows = {},
            }, Conn)
        end,
    }
    function fake_sq3.open()
        fake_sq3.last_conn = fake_sq3.new_conn()
        return fake_sq3.last_conn
    end
end
package.preload["lua-ljsqlite3/init"] = function() return fake_sq3 end

local annotations = require("bridge_annotations")
local coordinator_module = require("bridge_sync_coordinator")
local stats_batches = require("bridge_stats_batches")
local version = require("bridge_version")
local sessions = require("bridge_sessions")
local BridgeSqliteState = require("bridge_sqlite_state")
local APIClient = require("bridge_api_client")
local TransferPolicy = require("bridge_transfer_policy")
local BridgeSweep = require("bridge_sweep")

local small_block, small_total = TransferPolicy.timeouts(1024)
local large_block, large_total = TransferPolicy.timeouts(64 * 1024 * 1024)
assert(small_block == 30 and large_block == 30,
    "the block timeout must remain a stall detector")
assert(large_total > small_total, "download total timeout must scale with manifest size")
assert(TransferPolicy.maxBytes(1024) < TransferPolicy.maxBytes(nil),
    "known-size downloads must use a tighter byte ceiling")

local request_runner_calls = 0
local api_client = APIClient:new()
api_client:init("http://bridge", "reader", "secret", nil, function(task)
    request_runner_calls = request_runner_calls + 1
    return true, task()
end)
local request_ok, request_code, request_body = api_client:_request("GET", "/test")
assert(request_ok and request_code == 200 and request_body == "ok",
    "API request must preserve the HTTP result")
-- Forking a ~100 MB reader image for every API call is what exhausted memory on
-- Kindle ("fork failed: Cannot allocate memory") and made each child re-open the
-- plugin database behind its parent ("database is locked"). Ordinary requests
-- must stay on the UI loop and never reach the subprocess runner.
assert(request_runner_calls == 0, "ordinary HTTP must not fork a subprocess")

local opted_in_ok, opted_in_code = api_client:_request("GET", "/test", nil, nil, { background = true })
assert(opted_in_ok and opted_in_code == 200,
    "an explicit background request must still return its HTTP result")
assert(request_runner_calls == 1, "background = true must still route through the request runner")

local download_path = os.tmpname()
local download_ok, download_err = api_client:downloadBook("/book", download_path, 2)
assert(download_ok, download_err)
local downloaded = assert(io.open(download_path, "rb"))
assert(downloaded:read("*a") == "ok", "book download must publish the expected bytes")
downloaded:close()
os.remove(download_path)

local mismatch_path = os.tmpname()
local mismatch_ok, mismatch_err = api_client:downloadBook("/book", mismatch_path, 3)
assert(not mismatch_ok and mismatch_err == "download_size_mismatch",
    "manifest/download size mismatch must fail closed")
local mismatch_file = io.open(mismatch_path, "rb")
assert(not mismatch_file, "failed size validation must remove the partial download")

local plugin_path = os.tmpname()
local plugin_ok, plugin_digest = api_client:downloadPluginZip(plugin_path)
assert(plugin_ok and plugin_digest == fake_plugin_digest,
    "plugin download must preserve the server's archive digest")
local plugin_download = assert(io.open(plugin_path, "rb"))
assert(plugin_download:read("*a") == "ok", "plugin download must publish the response bytes")
plugin_download:close()
os.remove(plugin_path)

fake_http_content_length = #fake_http_body + 1
local short_plugin_path = os.tmpname()
local short_plugin_ok, short_plugin_err = api_client:downloadPluginZip(short_plugin_path)
assert(not short_plugin_ok and short_plugin_err == "download_size_mismatch",
    "incomplete plugin downloads must fail closed")
assert(not io.open(short_plugin_path, "rb"),
    "incomplete plugin downloads must remove their partial file")
fake_http_content_length = nil

local entries = {}
for index = 1, 55 do
    entries[index] = {
        datetime = "2020-01-01 00:00:00",
        datetime_updated = "2020-01-01 00:00:00",
        drawer = "lighten",
        text = "highlight " .. index,
        pos0 = "/body/p[" .. index .. "]/text().0",
        pos1 = "/body/p[" .. index .. "]/text().5",
    }
end

local saved_watermarks = {}
local saved_signatures = {}
local exchange_calls = 0
local exchange_payloads = {}
local bridge = {
    state = {
        readSetting = function(_, key)
            if key == "annotation_watermarks" then return saved_watermarks end
            if key == "annotation_signatures" then return saved_signatures end
            return nil
        end,
        saveSetting = function(_, key, value)
            if key == "annotation_watermarks" then saved_watermarks = value end
            if key == "annotation_signatures" then saved_signatures = value end
        end,
        flush = function() end,
    },
    api = {
        exchangeAnnotations = function(_, payload)
            exchange_calls = exchange_calls + 1
            exchange_payloads[#exchange_payloads + 1] = payload
            local response_books = {}
            for _, book in ipairs(payload.books) do
                table.insert(response_books, {
                    hash = book.hash,
                    toApply = { add = {}, edit = {}, delete = {} },
                    more = false,
                })
            end
            return true, { enabled = true, books = response_books }
        end,
        ackAnnotations = function() return true end,
    },
    _currentDeviceIdentity = function() return "Test", "device-1" end,
    logInfo = function() end,
    logWarn = function() end,
}

local normalize_calls = 0
local original_normalize = annotations.normalizeEntry
annotations.normalizeEntry = function(raw)
    normalize_calls = normalize_calls + 1
    return original_normalize(raw)
end
local result, exchange_err = annotations.exchangeBooks(bridge, {
    { hash = string.rep("a", 32), annotations = entries, live = false },
})
annotations.normalizeEntry = original_normalize
assert(result, exchange_err)
assert(result.uploaded == 55, "all annotation chunks must be uploaded")
assert(exchange_calls == 2, "55 annotations must be split across two exchanges")
assert(normalize_calls == 55, "annotations must be normalized only once per exchange")
assert(saved_watermarks[string.rep("a", 32)] == "2020-01-01 00:00:00",
    "watermark advances only after all same-timestamp chunks succeed")
assert(type(saved_signatures[string.rep("a", 32)]) == "string",
    "complete local annotation sets must persist a signature")

exchange_calls = 0
exchange_payloads = {}
local unchanged_result, unchanged_err = annotations.exchangeBooks(bridge, {
    { hash = string.rep("a", 32), annotations = entries, live = false },
})
assert(unchanged_result, unchanged_err)
assert(exchange_calls == 1, "unchanged annotations still require one pull-only exchange")
assert(exchange_payloads[1].books[1].keysComplete == false
        and #exchange_payloads[1].books[1].keys == 0,
    "unchanged annotation signatures must suppress the complete key list")

local identity_entries = {
    { datetime = "same", pos0 = "/body/p[1]", text = "one" },
    { datetime = "same", pos0 = "/body/p[2]", text = "two" },
}
local identity = annotations.newIdentityIndex(identity_entries)
assert(identity:find({ datetime = "same", pos0 = "/body/p[2]", text = "two" }) == 2,
    "ambiguous datetimes must fall back to indexed position and text")

local bounded_calls = 0
local bounded_sizes = {}
bridge.api.max_json_body_bytes = 1500
bridge.api.jsonBodySize = function(_, payload)
    local size = 100
    for _, book in ipairs(payload.books or {}) do
        size = size + #(book.keys or {}) * 40 + #(book.changes or {}) * 100
    end
    return size
end
bridge.api.exchangeAnnotations = function(_, payload)
    bounded_calls = bounded_calls + 1
    local size = bridge.api:jsonBodySize(payload)
    bounded_sizes[#bounded_sizes + 1] = size
    local response_books = {}
    for _, book in ipairs(payload.books) do
        response_books[#response_books + 1] = {
            hash = book.hash,
            toApply = { add = {}, edit = {}, delete = {} },
            more = false,
        }
    end
    return true, { enabled = true, books = response_books }
end
local bounded_entries = {}
for index = 1, 20 do
    bounded_entries[index] = {
        datetime = "2021-01-01 00:00:00",
        pos0 = "/body/bounded[" .. index .. "]",
        pos1 = "/body/bounded[" .. index .. "]/end",
    }
end
local bounded_result, bounded_err = annotations.exchangeBooks(bridge, {
    { hash = string.rep("b", 32), annotations = bounded_entries, live = false },
})
assert(bounded_result, bounded_err)
assert(bounded_result.uploaded == 20 and bounded_calls > 1,
    "byte budgeting must split an otherwise oversized annotation exchange")
for _, size in ipairs(bounded_sizes) do
    assert(size <= bridge.api.max_json_body_bytes, "annotation exchange exceeded its byte budget")
end

fake_history = {
    { file = "/books/one.epub" },
    { file = "/books/two.epub" },
    { file = "/books/three.epub" },
}
fake_lfs.attributes = function() return "file" end
fake_docsettings.hasSidecarFile = function() return true end
local original_resolve_hash = annotations.resolveBookHash
local original_exchange_books = annotations.exchangeBooks
annotations.resolveBookHash = function(file) return string.rep(file:sub(8, 8), 32) end
local sweep_exchanges = 0
annotations.exchangeBooks = function()
    sweep_exchanges = sweep_exchanges + 1
    return { uploaded = 1, applied = 0, deleted = 0 }
end
local sweep_saved, sweep_done
local sweep_bridge = {
    state = {
        readSetting = function() return sweep_saved end,
        saveSetting = function(_, _, value) sweep_saved = value end,
        delSetting = function() sweep_saved = nil end,
        flush = function() end,
    },
    logInfo = function() end,
    logWarn = function() end,
}
assert(BridgeSweep.start(sweep_bridge, nil, function(_, message) sweep_done = message or "done" end))
assert(sweep_exchanges == 0, "sweep history discovery must yield before network work")
while #scheduled_callbacks > 0 do
    local callback = table.remove(scheduled_callbacks, 1)
    callback()
end
assert(sweep_exchanges == 3 and sweep_done == "done" and sweep_saved == nil,
    "sweep must process and ack-gate every queued history book")

sweep_done = nil
assert(BridgeSweep.start(sweep_bridge, nil, function(_, message) sweep_done = message end))
BridgeSweep.cancel()
while #scheduled_callbacks > 0 do
    local callback = table.remove(scheduled_callbacks, 1)
    callback()
end
assert(sweep_exchanges == 3 and sweep_done == "cancelled",
    "sweep cancellation must invalidate scheduled work before exchange")
annotations.resolveBookHash = original_resolve_hash
annotations.exchangeBooks = original_exchange_books

local pages, books = {}, {}
for index = 1, 10001 do
    local hash = string.format("%032d", (index % 3) + 1)
    pages[index] = { md5 = hash, page = index }
end
for index = 1, 3 do
    books[index] = { md5 = string.format("%032d", index), title = "Book " .. index }
end
local batches = stats_batches.build(pages, books, 3000)
assert(#batches == 4, "10001 rows must produce four bounded batches")
local page_count = 0
for _, batch in ipairs(batches) do
    assert(#batch.page_stats <= 3000, "statistics batch exceeded its limit")
    page_count = page_count + #batch.page_stats
end
assert(page_count == 10001, "statistics batching lost rows")

local now = 100
local coordinator = coordinator_module:new(function() return now end)
local order = {}
local finish_first
coordinator:submit({
    family = "first", priority = 100,
    run = function(done)
        table.insert(order, "first")
        finish_first = done
    end,
})
coordinator:submit({
    family = "annotations", priority = 100,
    run = function(done) table.insert(order, "old-annotations"); done() end,
})
coordinator:submit({
    family = "annotations", priority = 200,
    run = function(done) table.insert(order, "new-annotations"); done() end,
})
coordinator:submit({
    family = "close", priority = 300,
    run = function(done) table.insert(order, "close"); done() end,
})
assert(coordinator:status().pending_count == 2, "duplicate family was not coalesced")
finish_first()
assert(table.concat(order, ",") == "first,close,new-annotations",
    "coordinator did not honor priority and replacement")
assert(not coordinator:isBusy(), "coordinator remained busy after all jobs completed")

-- Cancellation: suspend and ReaderUI teardown must be able to abandon work.
local owner_a, owner_b = {}, {}
local ran = {}
local finish_active
local cancel_coordinator = coordinator_module:new(function() return now end)
cancel_coordinator:submit({
    family = "active", owner = owner_a, priority = 100,
    run = function(done) table.insert(ran, "active"); finish_active = done end,
})
cancel_coordinator:submit({
    family = "queued_a", owner = owner_a, priority = 100,
    run = function(done) table.insert(ran, "queued_a"); done() end,
})
cancel_coordinator:submit({
    family = "queued_b", owner = owner_b, priority = 100,
    run = function(done) table.insert(ran, "queued_b"); done() end,
})
assert(cancel_coordinator:status().pending_count == 2, "cancellation fixture did not queue both jobs")
-- A departing instance drops only its own queued work; another owner's survives.
assert(cancel_coordinator:cancelPending(owner_a) == 1,
    "cancelPending did not drop the departing owner's queued job")
assert(cancel_coordinator:status().pending_count == 1,
    "cancelPending dropped another owner's queued job")
-- Teardown leaves a running job alone; only an explicit cancel flags it.
assert(not cancel_coordinator:isActiveCancelled(),
    "cancelPending must not flag the job that is already running")
assert(cancel_coordinator:cancelActive(owner_b) == false,
    "cancelActive flagged a job belonging to a different owner")
assert(cancel_coordinator:cancelActive(owner_a),
    "cancelActive did not flag the owner's running job")
assert(cancel_coordinator:isActiveCancelled(), "the running job was not reported as cancelled")
-- A flagged job still drains the queue when it finishes, so cancelling can
-- never wedge the coordinator.
finish_active()
assert(table.concat(ran, ",") == "active,queued_b", "cancellation ran the wrong jobs")
assert(not cancel_coordinator:isBusy(), "coordinator stayed busy after a cancelled job finished")

local purge_coordinator = coordinator_module:new(function() return now end)
local purge_finish
purge_coordinator:submit({
    family = "running", priority = 100,
    run = function(done) purge_finish = done end,
})
purge_coordinator:submit({
    family = "waiting", priority = 100,
    run = function() error("a purged job must never run") end,
})
assert(purge_coordinator:cancelPending() == 1, "cancelPending did not report the dropped job")
assert(purge_coordinator:status().pending_count == 0, "cancelPending left work queued")
assert(purge_coordinator:cancelActive(), "cancelActive did not flag the running job")
purge_finish()
assert(not purge_coordinator:isBusy(), "coordinator stayed busy after a purge")

assert(version.isNewer("0.4.0", "0.3.6"), "newer semantic version was not detected")
assert(not version.isNewer("0.3.5", "0.3.6"), "older server version would trigger a downgrade")
assert(not version.isNewer("0.3.6", "0.3.6"), "equal version was treated as newer")

-- ── ManifestRules (bridge_manifest_rules.lua) ──
local manifest_rules = require("bridge_manifest_rules")

assert(manifest_rules.revisionToPersist("abc123", 0, 0) == "abc123",
    "revisionToPersist clean sweep persists revision")
assert(manifest_rules.revisionToPersist("abc123", 1, 0) == "",
    "revisionToPersist with errors returns empty string")
assert(manifest_rules.revisionToPersist("abc123", 0, 2) == "",
    "revisionToPersist with remaining downloads returns empty string")
assert(manifest_rules.revisionToPersist("abc123", "3", nil) == "",
    "revisionToPersist coerces string errors to number")
assert(manifest_rules.revisionToPersist(nil, 0, 0) == "",
    "revisionToPersist with nil revision returns empty string")
assert(manifest_rules.revisionToPersist("abc123", nil, nil) == "abc123",
    "revisionToPersist treats nil errors/remaining as zero")

assert(manifest_rules.downloadAllowed(0, 0) == true, "downloadAllowed 0 cap is unlimited")
assert(manifest_rules.downloadAllowed(999, 0) == true, "downloadAllowed 0 cap unlimited even at high attempts")
assert(manifest_rules.downloadAllowed(0, nil) == true, "downloadAllowed nil cap is unlimited")
assert(manifest_rules.downloadAllowed(4, 5) == true, "downloadAllowed attempts under cap allowed")
assert(manifest_rules.downloadAllowed(5, 5) == false, "downloadAllowed attempts at cap denied")
assert(manifest_rules.downloadAllowed(nil, 5) == true, "downloadAllowed nil attempts treated as zero")
assert(manifest_rules.downloadAllowed(3, -1) == true, "downloadAllowed negative cap is unlimited")

-- ── Session collapsing (bridge_sessions.lua, the real module) ──

-- Adjacent sessions for the same book merge, and reading duration
-- accumulates instead of absorbing the idle gap between them.
do
    local pending = {}
    local first = {
        abs_id = "book-1", start_time = 1000, end_time = 1100,
        duration_seconds = 100, start_progress = 0, end_progress = 10, end_page = 5,
    }
    assert(not sessions.mergeIntoPending(pending, first, 300),
        "first session has nothing to merge into")
    table.insert(pending, first)

    local merged = sessions.mergeIntoPending(pending, {
        abs_id = "book-1", start_time = 1300, end_time = 1400,
        duration_seconds = 100, start_progress = 10, end_progress = 20, end_page = 10,
    }, 300)
    assert(merged, "session 200s after previous end must merge with 300s threshold")
    assert(#pending == 1, "merged sessions must collapse into one entry")
    assert(pending[1].end_time == 1400, "merge must extend end_time")
    assert(pending[1].end_progress == 20, "merge must extend end_progress")
    assert(pending[1].end_page == 10, "merge must extend end_page")
    assert(pending[1].duration_seconds == 200,
        "merged duration must be the sum of reading time, not the 1000-1400 span")
end

-- Different books never merge.
do
    local pending = {
        { abs_id = "book-1", start_time = 1000, end_time = 1100, duration_seconds = 100 },
    }
    local merged = sessions.mergeIntoPending(pending, {
        abs_id = "book-2", start_time = 1150, end_time = 1250, duration_seconds = 100,
    }, 300)
    assert(not merged, "different books must not merge")
end

-- Sessions past the threshold never merge.
do
    local pending = {
        { abs_id = "book-1", start_time = 1000, end_time = 1100, duration_seconds = 100 },
    }
    local merged = sessions.mergeIntoPending(pending, {
        abs_id = "book-1", start_time = 1500, end_time = 1600, duration_seconds = 100,
    }, 300)
    assert(not merged, "session 400s later must not merge with 300s threshold")
end

-- Hash-only sessions (no abs_id) never merge.
do
    local pending = {
        { abs_id = nil, document_hash = "h1", start_time = 1000, end_time = 1100, duration_seconds = 100 },
    }
    local merged = sessions.mergeIntoPending(pending, {
        abs_id = nil, document_hash = "h1", start_time = 1150, end_time = 1250, duration_seconds = 100,
    }, 300)
    assert(not merged, "nil abs_id sessions must always append")
end

-- Already-uploaded sessions are never merge targets.
do
    local pending = {
        { abs_id = "book-1", start_time = 1000, end_time = 1100, duration_seconds = 100, uploaded = true },
    }
    local merged = sessions.mergeIntoPending(pending, {
        abs_id = "book-1", start_time = 1150, end_time = 1250, duration_seconds = 100,
    }, 300)
    assert(not merged, "uploaded sessions must not absorb new reading time")
end

-- ── bridge_sqlite_state.lua contract tests (fake lua-ljsqlite3) ──
-- These pin the module to the real library semantics: positional bind1
-- parameters, step() returning nil on successful writes, close() cleanup.

local function newState()
    local state = BridgeSqliteState:new()
    assert(state:is_available(), "fake SQ3 must make sqlite available")
    assert(state:init(), "init must succeed against the fake connection")
    return state, fake_sq3.last_conn
end

local function lastStmt(conn)
    local stmt = conn.prepared[#conn.prepared]
    assert(stmt, "expected a prepared statement")
    assert(stmt.closed, "statements must be close()d after use")
    return stmt
end

-- Writes succeed: step() returning nil (SQLITE_DONE) is success, not failure.
do
    local state, conn = newState()
    assert(state:set_setting("server_url", "http://bridge:5758") == true,
        "set_setting must treat step()'s nil return (SQLITE_DONE) as success")
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("INSERT OR REPLACE INTO plugin_settings"),
        "set_setting must upsert plugin_settings")
    assert(stmt.params[1] == "bridgesync", "param 1 must be the plugin name")
    assert(stmt.params[2] == "server_url", "param 2 must be the key")
    assert(stmt.params[3] == "http://bridge:5758", "param 3 must be the value")
    assert(stmt.params[4] == "string", "param 4 must be the type tag")
end

-- Deleting via nil value.
do
    local state, conn = newState()
    assert(state:set_setting("stale_key", nil) == true)
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("DELETE FROM plugin_settings"), "nil value must delete the row")
    assert(stmt.params[2] == "stale_key")
end

-- Reads decode type tags; a stored boolean false survives as false.
do
    local state, conn = newState()
    conn:script_rows({ { "false", "boolean" } })
    local value = state:get_setting("auto_sync_on_close", true)
    assert(value == false,
        "stored boolean false must be returned as false, never the default")

    conn:script_rows({ { "42", "number" } })
    assert(state:get_setting("wake_sync_delay_seconds") == 42, "number decode failed")

    assert(state:get_setting("missing_key", "fallback") == "fallback",
        "missing key must return the default")
end

-- Pending sessions persist every field the bridge upload endpoint consumes.
do
    local state, conn = newState()
    local sid = state:add_pending_session({
        abs_id = "abs-1",
        document_hash = "deadbeef",
        session_type = "EPUB",
        start_time = 1000,
        end_time = 1600,
        duration_seconds = 600,
        start_page = 10,
        end_page = 25,
        start_progress = 12.5,
        end_progress = 20.0,
    })
    assert(type(sid) == "string" and sid ~= "", "add_pending_session must return an id")
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("INSERT INTO plugin_pending_sessions"))
    assert(stmt.params[1] == "bridgesync")
    assert(stmt.params[2] == sid)
    assert(stmt.params[3] == "abs-1")
    assert(stmt.params[4] == "deadbeef", "document_hash must be persisted")
    assert(stmt.params[5] == "EPUB", "session_type must be persisted")
    assert(stmt.params[6] == 1000 and stmt.params[7] == 1600)
    assert(stmt.params[8] == 600, "duration_seconds must be persisted")
    assert(stmt.params[9] == 10 and stmt.params[10] == 25)
    assert(stmt.params[11] == 12.5 and stmt.params[12] == 20.0)
end

-- Session rows come back in upload-payload shape.
do
    local state, conn = newState()
    conn:script_rows({
        { "sid-1", "abs-1", "deadbeef", "EPUB", 1000, 1600, 600, 10, 25, 12.5, 20.0, 0 },
    })
    local pending = state:get_pending_sessions(nil, false)
    assert(#pending == 1)
    local s = pending[1]
    assert(s.session_id == "sid-1" and s.abs_id == "abs-1")
    assert(s.document_hash == "deadbeef" and s.session_type == "EPUB")
    assert(s.start_time == 1000 and s.end_time == 1600 and s.duration_seconds == 600)
    assert(s.uploaded == false, "uploaded flag must decode to boolean")
    local stmt = lastStmt(conn)
    assert(stmt.params[2] == 0, "uploaded=false filter must bind 0")
end

-- Merging extends the end state and ADDS reading duration.
do
    local state, conn = newState()
    assert(state:merge_pending_session_end("sid-1", 1600, 25, 20.0, 300) == true)
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("duration_seconds = duration_seconds %+ %?"),
        "merge must accumulate duration, not overwrite it")
    assert(stmt.params[1] == 1600 and stmt.params[2] == 25 and stmt.params[3] == 20.0)
    assert(stmt.params[4] == 300, "param 4 must be the added duration")
    assert(stmt.params[6] == "sid-1")
end

-- find_mergeable guards and parameter order.
do
    local state, conn = newState()
    assert(state:find_mergeable_pending_session(nil, 1000, 300) == nil,
        "nil abs_id must never find a merge target")
    conn:script_rows({
        { "sid-2", "abs-1", nil, "EPUB", 500, 900, 400, 1, 9, 0.0, 10.0, 0 },
    })
    local found = state:find_mergeable_pending_session("abs-1", 1000, 300)
    assert(found and found.session_id == "sid-2")
    local stmt = lastStmt(conn)
    assert(stmt.params[2] == "abs-1" and stmt.params[3] == 1000 and stmt.params[4] == 300)
end

-- Batch upload marking binds ids after the plugin name.
do
    local state, conn = newState()
    assert(state:mark_sessions_uploaded({ "a", "b", "c" }) == true)
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("SET uploaded = 1"))
    assert(stmt.params[1] == "bridgesync")
    assert(stmt.params[2] == "a" and stmt.params[3] == "b" and stmt.params[4] == "c")
    assert(state:mark_sessions_uploaded({}) == false, "empty batch must be a no-op failure")
end

print("BridgeSync Lua core tests passed")
