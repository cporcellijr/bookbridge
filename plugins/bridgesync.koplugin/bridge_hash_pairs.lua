--[[
Content and filename hashes of the books in KOReader's history.

In filename mode the bridge knows a reader's own copy only by the hash of its
file name, while KOReader's statistics, statuses and highlights name it by its
content hash. Reporting both lets the bridge link the content hash to the book.

The content hash is read from the sidecar, never computed: hashing a file means
reading it, and a book that was never opened has no statistics or status to link.
--]]

local DocSettings = require("docsettings")
local lfs = require("libs/libkoreader-lfs")
local md5 = require("ffi/sha2").md5

local HashPairs = {}

local DEFAULT_MAX_HISTORY = 200

local function storedHash(file)
    if not DocSettings:hasSidecarFile(file) then
        return nil
    end
    local ok, doc_settings = pcall(DocSettings.open, DocSettings, file)
    if not ok or not doc_settings then
        return nil
    end
    local stored = doc_settings:readSetting("partial_md5_checksum")
    if type(stored) == "string" and #stored == 32 and stored:match("^%x+$") then
        return stored:lower()
    end
    return nil
end

-- @param history_items table  ReadHistory.hist-shaped list of { file = path }
-- @param opts table|nil       { max = number of history entries to read }
-- @treturn table pairs  { { content_hash, filename_hash, filename }, ... }
-- @treturn table index  content_hash -> path
function HashPairs.collect(history_items, opts)
    opts = opts or {}
    local max = tonumber(opts.max) or DEFAULT_MAX_HISTORY
    local found, index, seen = {}, {}, {}
    for position, item in ipairs(history_items or {}) do
        if position > max then break end
        local file = type(item) == "table" and item.file or nil
        if type(file) == "string" and not seen[file] then
            seen[file] = true
            if lfs.attributes(file, "mode") == "file" then
                local content_hash = storedHash(file)
                local filename = file:match("([^/]*)$")
                if content_hash and filename and filename ~= "" then
                    table.insert(found, {
                        content_hash = content_hash,
                        filename_hash = md5(filename),
                        filename = filename,
                    })
                    index[content_hash] = index[content_hash] or file
                end
            end
        end
    end
    return found, index
end

return HashPairs
