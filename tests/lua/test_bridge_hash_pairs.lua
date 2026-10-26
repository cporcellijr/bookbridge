-- Executable coverage for bridge_hash_pairs.lua: the content and filename hashes
-- of the books in KOReader's history, read from sidecars without touching files.

local plugin_dir = assert(arg[1], "plugin directory argument required")
package.path = plugin_dir .. "/?.lua;" .. package.path

-- path -> sidecar settings; a path absent here has no sidecar.
local sidecars = {}
-- paths that exist on disk.
local files = {}

package.preload["docsettings"] = function()
    return {
        hasSidecarFile = function(_, file) return sidecars[file] ~= nil end,
        open = function(_, file)
            local store = sidecars[file] or {}
            return { readSetting = function(_, key) return store[key] end }
        end,
    }
end
package.preload["libs/libkoreader-lfs"] = function()
    return { attributes = function(path, attr) return files[path] and attr == "mode" and "file" or nil end }
end
package.preload["ffi/sha2"] = function()
    return { md5 = function(value) return "md5:" .. value end }
end

local HashPairs = require("bridge_hash_pairs")

local OWN = "/storage/emulated/0/Documents/David Scott Hay - The Butcher of Nazareth.epub"
local CHECKSUM = "B8B32D8752F6CBB3E6AD4BDC76C3B366"

local function reset()
    sidecars, files = {}, {}
end

-- A history book with a sidecar yields one pair and an index entry.
reset()
files[OWN] = true
sidecars[OWN] = { partial_md5_checksum = CHECKSUM }
local pairs_out, index = HashPairs.collect({ { file = OWN } })
assert(#pairs_out == 1, "expected one pair, got " .. #pairs_out)
assert(pairs_out[1].content_hash == CHECKSUM:lower(), "content hash must come from the sidecar, lowercased")
assert(pairs_out[1].filename_hash == "md5:David Scott Hay - The Butcher of Nazareth.epub",
    "filename hash must be the md5 of the basename")
assert(pairs_out[1].filename == "David Scott Hay - The Butcher of Nazareth.epub")
assert(index[CHECKSUM:lower()] == OWN, "index must map the content hash to the file")

-- No sidecar, a missing file, or a malformed stored hash yields nothing.
reset()
local unopened = "/storage/emulated/0/Documents/Unopened.epub"
local deleted = "/storage/emulated/0/Documents/Deleted.epub"
local malformed = "/storage/emulated/0/Documents/Malformed.epub"
files[unopened] = true
sidecars[deleted] = { partial_md5_checksum = CHECKSUM }
files[malformed] = true
sidecars[malformed] = { partial_md5_checksum = "not-a-hash" }
pairs_out = HashPairs.collect({ { file = unopened }, { file = deleted }, { file = malformed }, "junk" })
assert(#pairs_out == 0, "unusable history entries must be skipped, got " .. #pairs_out)

-- The same path twice appears once.
reset()
files[OWN] = true
sidecars[OWN] = { partial_md5_checksum = CHECKSUM }
pairs_out = HashPairs.collect({ { file = OWN }, { file = OWN } })
assert(#pairs_out == 1, "a repeated path must appear once")

-- The cap bounds how much history is read.
reset()
local history = {}
for i = 1, 5 do
    local path = "/books/" .. i .. ".epub"
    files[path] = true
    sidecars[path] = { partial_md5_checksum = string.format("%032x", i) }
    history[i] = { file = path }
end
pairs_out = HashPairs.collect(history, { max = 3 })
assert(#pairs_out == 3, "the cap must bound the pairs, got " .. #pairs_out)

print("BridgeSync hash-pairs Lua tests passed")
