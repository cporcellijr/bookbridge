local BridgeVersion = {}

local function parse(value)
    local major, minor, patch = tostring(value or ""):match("^v?(%d+)%.(%d+)%.(%d+)")
    if not major then return nil end
    return { tonumber(major), tonumber(minor), tonumber(patch) }
end

function BridgeVersion.isNewer(candidate, current)
    local next_version, installed = parse(candidate), parse(current)
    if not next_version or not installed then return false end
    for index = 1, 3 do
        if next_version[index] ~= installed[index] then
            return next_version[index] > installed[index]
        end
    end
    return false
end

return BridgeVersion
