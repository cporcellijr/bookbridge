local plugin_dir = assert(arg[1], "plugin directory argument required")
package.path = plugin_dir .. "/?.lua;" .. package.path

package.preload["docsettings"] = function() return {} end
package.preload["libs/libkoreader-lfs"] = function() return { attributes = function() return nil end } end
package.preload["logger"] = function() return { warn = function() end } end
package.preload["ffi/sha2"] = function()
    return { md5 = function(value) return tostring(value):sub(1, 32) end }
end

local annotations = require("bridge_annotations")
local coordinator_module = require("bridge_sync_coordinator")
local stats_batches = require("bridge_stats_batches")
local version = require("bridge_version")

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
local exchange_calls = 0
local bridge = {
    state = {
        readSetting = function(_, key)
            if key == "annotation_watermarks" then return saved_watermarks end
            return nil
        end,
        saveSetting = function(_, key, value)
            if key == "annotation_watermarks" then saved_watermarks = value end
        end,
        flush = function() end,
    },
    api = {
        exchangeAnnotations = function(_, payload)
            exchange_calls = exchange_calls + 1
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

local result, exchange_err = annotations.exchangeBooks(bridge, {
    { hash = string.rep("a", 32), annotations = entries, live = false },
})
assert(result, exchange_err)
assert(result.uploaded == 55, "all annotation chunks must be uploaded")
assert(exchange_calls == 2, "55 annotations must be split across two exchanges")
assert(saved_watermarks[string.rep("a", 32)] == "2020-01-01 00:00:00",
    "watermark advances only after all same-timestamp chunks succeed")

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

assert(version.isNewer("0.4.0", "0.3.6"), "newer semantic version was not detected")
assert(not version.isNewer("0.3.5", "0.3.6"), "older server version would trigger a downgrade")
assert(not version.isNewer("0.3.6", "0.3.6"), "equal version was treated as newer")

print("BridgeSync Lua core tests passed")
