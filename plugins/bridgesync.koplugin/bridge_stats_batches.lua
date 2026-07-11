-- Builds bounded statistics upload batches without losing book metadata.

local StatsBatches = {}

function StatsBatches.build(page_stats, books, batch_size)
    local result = {}
    local size = math.max(tonumber(batch_size) or 1, 1)
    local start_index = 1
    while start_index <= #(page_stats or {}) do
        local rows, included_md5 = {}, {}
        local stop_index = math.min(#page_stats, start_index + size - 1)
        for index = start_index, stop_index do
            local row = page_stats[index]
            table.insert(rows, row)
            if row.md5 then included_md5[row.md5] = true end
        end
        local book_rows = {}
        for _, book in ipairs(books or {}) do
            if included_md5[book.md5] then table.insert(book_rows, book) end
        end
        table.insert(result, { page_stats = rows, books = book_rows })
        start_index = stop_index + 1
    end
    return result
end

return StatsBatches
