local socket = require("socket")
local http = require("socket.http")
local ltn12 = require("ltn12")
local json = require("json")
local logger = require("logger")
local socketutil = require("socketutil")
local TransferPolicy = require("bridge_transfer_policy")

local KOSYNC_ACCEPT = "application/vnd.koreader.v1+json"
local MAX_JSON_BODY_BYTES = 900 * 1024
local MAX_PLUGIN_ZIP_BYTES = 16 * 1024 * 1024

-- A JSON null decodes to a non-nil sentinel (a function/userdata in KOReader's
-- json lib), which is truthy and, if it reaches a KOReader annotation field,
-- crashes rendering ("attempt to concatenate a function value"). Recursively
-- drop anything that isn't a string/number/boolean/table so absent optional
-- fields read as nil.
local function scrubJsonNulls(value)
    local t = type(value)
    if t == "table" then
        for k, v in pairs(value) do
            value[k] = scrubJsonNulls(v)
        end
        return value
    elseif t == "string" or t == "number" or t == "boolean" then
        return value
    end
    return nil
end

local APIClient = {
    server_url = "",
    username = "",
    key = "",
    timeout = 10,
    max_json_body_bytes = MAX_JSON_BODY_BYTES,
    log_callback = nil,
    request_runner = nil,
}

function APIClient:new(o)
    o = o or {}
    setmetatable(o, self)
    self.__index = self
    return o
end

function APIClient:init(server_url, username, key, log_callback, request_runner)
    self.server_url = tostring(server_url or ""):gsub("/+$", "")
    self.username = tostring(username or "")
    self.key = tostring(key or "")
    self.log_callback = log_callback
    self.request_runner = request_runner
end

function APIClient:_performRequest(request_builder, block_timeout, total_timeout, background)
    local task = function()
        socketutil:set_timeout(block_timeout, total_timeout)
        local request, response_body = request_builder()
        local code, response_headers, status = socket.skip(1, http.request(request))
        socketutil:reset_timeout()
        return code, response_headers, status,
            response_body and table.concat(response_body) or nil
    end
    if background and type(self.request_runner) == "function" then
        local runner_ok, code, response_headers, status, body = self.request_runner(task)
        if not runner_ok then return nil, nil, tostring(code or "subprocess failed"), nil end
        return code, response_headers, status, body
    end
    return task()
end

function APIClient:_log(level, ...)
    local parts = {}
    for i = 1, select("#", ...) do
        parts[#parts + 1] = tostring(select(i, ...))
    end
    local message = table.concat(parts, " ")
    if level == "error" then
        logger.err("Bridge Sync API:", message)
    elseif level == "warn" then
        logger.warn("Bridge Sync API:", message)
    else
        logger.info("Bridge Sync API:", message)
    end
    if self.log_callback then
        self.log_callback(level, message)
    end
end

function APIClient:_build_headers(extra_headers)
    local headers = extra_headers or {}
    headers["accept"] = KOSYNC_ACCEPT
    if self.username ~= "" and self.key ~= "" then
        headers["x-auth-user"] = self.username
        headers["x-auth-key"] = self.key
    end
    return headers
end

function APIClient:_request(method, path, sink, extra_headers, timeout_opts)
    if self.server_url == "" then
        return false, nil, "Server URL not configured"
    end

    local url = self.server_url .. path
    self:_log("info", method, url)
    local opts = timeout_opts or {}
    local block_timeout = opts.block_timeout or (sink and 60 or self.timeout)
    local total_timeout = opts.total_timeout or (sink and 300 or 30)
    local attempts = opts.attempts or 1
    local background = opts.background == true

    for attempt = 1, attempts do
        local code, response_headers, status, body = self:_performRequest(function()
            local response_body = sink and nil or {}
            return {
                url = url,
                method = method,
                headers = self:_build_headers(extra_headers),
                sink = sink or socketutil.table_sink(response_body),
            }, response_body
        end, block_timeout, total_timeout, background)

        local is_timeout = code == socketutil.TIMEOUT_CODE or
            code == socketutil.SSL_HANDSHAKE_CODE or
            code == socketutil.SINK_TIMEOUT_CODE
        -- A connection failure (route not up yet right after wake, DNS blip, etc.) comes
        -- back with no headers; retry it like a timeout instead of giving up immediately.
        local is_conn_failure = (not is_timeout) and response_headers == nil

        if is_timeout or is_conn_failure then
            local reason = tostring(status or code or "Connection failed")
            self:_log("warn", is_timeout and "Request interrupted:" or "Connection failed:", reason)
            if attempt < attempts then
                self:_log("info", "Retrying request", tostring(attempt + 1), "of", tostring(attempts))
                socket.sleep(math.min(attempt, 2))
            else
                return false, nil, reason
            end
        else
            if type(code) ~= "number" then
                self:_log("warn", "Non-numeric response code:", tostring(code))
                return false, nil, tostring(code)
            end

            if code >= 200 and code < 300 then
                return true, code, body, response_headers, status
            end
            self:_log("warn", "HTTP failure:", tostring(code), tostring(body or status or ""))
            return false, code, body or status or ("HTTP " .. tostring(code)), response_headers, status
        end
    end

    return false, nil, "Request failed"
end

function APIClient:testAuth()
    local ok, code, body = self:_request("GET", "/koreader/users/auth", nil, nil, {
        block_timeout = 20,
        total_timeout = 45,
        attempts = 2,
    })
    if not ok then
        return false, "Auth failed: " .. tostring(code or body or "Unknown error")
    end

    local ok2, _, body2 = self:_request("GET", "/koreader/device-sync/plugin/version", nil, nil, {
        block_timeout = 20,
        total_timeout = 45,
        attempts = 2,
    })
    if ok2 then
        local parsed, result = pcall(json.decode, body2 or "{}")
        if parsed and type(result) == "table" and result.name == "bridgesync" then
            local version_str = tostring(result.version or "unknown")
            return true, "Connected to BookBridge (plugin " .. version_str .. ")"
        end
    end
    return false, "Signed in, but this server did not respond as BookBridge (device-sync API unavailable). Check the server URL."
end

function APIClient:getManifest()
    local ok, code, body = self:_request("GET", "/koreader/device-sync/manifest", nil, nil, {
        block_timeout = 45,
        total_timeout = 120,
        attempts = 3,
    })
    if not ok then
        return false, body or ("HTTP " .. tostring(code))
    end

    local parsed, result = pcall(json.decode, body or "{}")
    if not parsed or type(result) ~= "table" then
        logger.warn("Bridge Sync API: Invalid manifest JSON")
        return false, "Invalid manifest response"
    end
    return true, result
end

function APIClient:downloadBook(download_path, save_path, expected_bytes)
    local attempts = 3
    local block_timeout, total_timeout = TransferPolicy.timeouts(expected_bytes)
    local max_bytes = TransferPolicy.maxBytes(expected_bytes)
    for attempt = 1, attempts do
        local handle, open_err = io.open(save_path, "wb")
        if not handle then
            return false, open_err or "Failed to open output file"
        end

        local received = 0
        local too_large = false
        local file_sink = socketutil.file_sink(handle)
        local bounded_sink = function(chunk, err)
            if chunk and chunk ~= "" then
                received = received + #chunk
                if received > max_bytes then
                    too_large = true
                    return nil, "response_too_large"
                end
            end
            return file_sink(chunk, err)
        end
        local ok, code, body, response_headers = self:_request("GET", download_path, bounded_sink, {
            ["accept-encoding"] = "identity",
        }, {
            block_timeout = block_timeout,
            total_timeout = total_timeout,
            attempts = 1,
        })
        pcall(function() handle:close() end)

        local content_length = response_headers and tonumber(
            response_headers["content-length"] or response_headers["Content-Length"])
        if content_length and content_length > max_bytes then too_large = true end
        local expected = tonumber(expected_bytes)
        if ok and not too_large and (not expected or expected <= 0 or received == expected) then
            return true
        end
        if too_large then body = "response_too_large" end
        if ok then body = "download_size_mismatch" end

        os.remove(save_path)
        if body ~= socketutil.TIMEOUT_CODE and
           body ~= socketutil.SSL_HANDSHAKE_CODE and
           body ~= socketutil.SINK_TIMEOUT_CODE
        then
            return false, body or ("HTTP " .. tostring(code))
        end

        if attempt < attempts then
            self:_log("info", "Retrying download", tostring(attempt + 1), "of", tostring(attempts), download_path)
            socket.sleep(attempt)
        else
            return false, body or ("HTTP " .. tostring(code))
        end
    end

    return false, "Download failed"
end

function APIClient:_requestJSON(method, path, json_body, timeout_opts)
    if self.server_url == "" then
        return false, nil, "Server URL not configured"
    end
    if type(json_body) ~= "string" then
        return false, nil, "Invalid JSON request body"
    end
    if #json_body > MAX_JSON_BODY_BYTES then
        return false, nil, "Request body is too large"
    end

    local url = self.server_url .. path
    self:_log("info", method, url)
    local opts = timeout_opts or {}
    local block_timeout = opts.block_timeout or self.timeout
    local total_timeout = opts.total_timeout or 30
    local attempts = opts.attempts or 1
    local background = opts.background == true

    for attempt = 1, attempts do
        local code, response_headers, status, body = self:_performRequest(function()
            local response_body = {}
            return {
                url = url,
                method = method,
                headers = self:_build_headers({
                    ["content-type"] = "application/json",
                    ["content-length"] = tostring(#json_body),
                }),
                source = ltn12.source.string(json_body),
                sink = socketutil.table_sink(response_body),
            }, response_body
        end, block_timeout, total_timeout, background)

        local is_timeout = code == socketutil.TIMEOUT_CODE or
            code == socketutil.SSL_HANDSHAKE_CODE or
            code == socketutil.SINK_TIMEOUT_CODE
        -- A connection failure (route not up yet right after wake, DNS blip, etc.) comes
        -- back with no headers; retry it like a timeout instead of giving up immediately.
        local is_conn_failure = (not is_timeout) and response_headers == nil

        if is_timeout or is_conn_failure then
            local reason = tostring(status or code or "Connection failed")
            self:_log("warn", is_timeout and "Request interrupted:" or "Connection failed:", reason)
            if attempt < attempts then
                self:_log("info", "Retrying request", tostring(attempt + 1), "of", tostring(attempts))
                socket.sleep(math.min(attempt, 2))
            else
                return false, nil, reason
            end
        else
            if type(code) ~= "number" then
                self:_log("warn", "Non-numeric response code:", tostring(code))
                return false, nil, tostring(code)
            end

            if code >= 200 and code < 300 then
                return true, code, body, response_headers, status
            end
            self:_log("warn", "HTTP failure:", tostring(code), tostring(body or status or ""))
            return false, code, body or status or ("HTTP " .. tostring(code)), response_headers, status
        end
    end

    return false, nil, "Request failed"
end

function APIClient:jsonBodySize(payload)
    local ok, encoded = pcall(json.encode, payload)
    if not ok or type(encoded) ~= "string" then
        return nil, tostring(encoded or "JSON encoding failed")
    end
    return #encoded
end

function APIClient:getPluginVersion()
    local ok, code, body = self:_request("GET", "/koreader/device-sync/plugin/version", nil, nil, {
        block_timeout = 20,
        total_timeout = 45,
        attempts = 2,
    })
    if not ok then
        return false, body or ("HTTP " .. tostring(code))
    end
    local parsed, result = pcall(json.decode, body or "{}")
    if not parsed or type(result) ~= "table" then
        logger.warn("Bridge Sync API: Invalid plugin version JSON")
        return false, "Invalid version response"
    end
    return true, result
end

function APIClient:downloadPluginZip(save_path)
    local attempts = 3
    for attempt = 1, attempts do
        local handle, open_err = io.open(save_path, "wb")
        if not handle then
            return false, open_err or "Failed to open output file"
        end

        local received = 0
        local too_large = false
        local file_sink = socketutil.file_sink(handle)
        local bounded_sink = function(chunk, err)
            if chunk and chunk ~= "" then
                received = received + #chunk
                if received > MAX_PLUGIN_ZIP_BYTES then
                    too_large = true
                    return nil, "response_too_large"
                end
            end
            return file_sink(chunk, err)
        end
        local ok, code, body, response_headers = self:_request(
            "GET", "/koreader/device-sync/plugin/download", bounded_sink,
            { ["accept-encoding"] = "identity" }, {
            block_timeout = 60,
            total_timeout = 300,
            attempts = 1,
        })
        pcall(function() handle:close() end)

        local content_length = response_headers and tonumber(
            response_headers["content-length"] or response_headers["Content-Length"])
        if content_length and content_length > MAX_PLUGIN_ZIP_BYTES then too_large = true end
        if ok and not too_large and (not content_length or received == content_length) then
            local digest = response_headers and (
                response_headers["x-content-sha256"] or response_headers["X-Content-SHA256"])
            return true, digest
        end
        if too_large then body = "response_too_large" end
        if ok then body = "download_size_mismatch" end

        os.remove(save_path)
        if body ~= socketutil.TIMEOUT_CODE and
           body ~= socketutil.SSL_HANDSHAKE_CODE and
           body ~= socketutil.SINK_TIMEOUT_CODE
        then
            return false, body or ("HTTP " .. tostring(code))
        end

        if attempt < attempts then
            self:_log("info", "Retrying plugin zip download", tostring(attempt + 1), "of", tostring(attempts))
            socket.sleep(attempt)
        else
            return false, body or ("HTTP " .. tostring(code))
        end
    end
    return false, "Download failed"
end

function APIClient:uploadSessions(sessions)
    local body = json.encode(sessions)
    return self:_requestJSON("POST", "/koreader/device-sync/sessions", body, {
        block_timeout = 20,
        total_timeout = 60,
        attempts = 2,
    })
end

function APIClient:uploadClientLogs(payload)
    local body = json.encode(payload)
    return self:_requestJSON("POST", "/koreader/device-sync/logs", body, {
        block_timeout = 5,
        total_timeout = 10,
        attempts = 1,
    })
end

function APIClient:uploadStatistics(payload)
    local body = json.encode(payload)
    return self:_requestJSON("POST", "/koreader/device-sync/statistics", body, {
        block_timeout = 30,
        total_timeout = 90,
        attempts = 2,
    })
end

function APIClient:exchangeAnnotations(payload)
    local body = json.encode(payload)
    local ok, code, resp_body = self:_requestJSON("POST", "/koreader/device-sync/annotations/exchange", body, {
        block_timeout = 30,
        total_timeout = 90,
        attempts = 2,
    })
    if not ok then
        return false, resp_body or ("HTTP " .. tostring(code))
    end
    local parsed, result = pcall(json.decode, resp_body or "{}")
    if not parsed or type(result) ~= "table" then
        logger.warn("Bridge Sync API: Invalid annotation exchange JSON")
        return false, "Invalid annotation exchange response"
    end
    return true, scrubJsonNulls(result)
end

function APIClient:ackAnnotations(payload)
    local body = json.encode(payload)
    local ok, code, resp_body = self:_requestJSON("POST", "/koreader/device-sync/annotations/exchange-ack", body, {
        block_timeout = 20,
        total_timeout = 60,
        attempts = 2,
    })
    if not ok then
        return false, resp_body or ("HTTP " .. tostring(code))
    end
    return true
end

local function _urlencode(value)
    return tostring(value or ""):gsub("[^%w%-%.%_%~]", function(char)
        return string.format("%%%02X", string.byte(char))
    end)
end

function APIClient:getMergedStatistics(device, device_id, since)
    local path = "/koreader/device-sync/statistics/merged"
        .. "?device=" .. _urlencode(device)
        .. "&device_id=" .. _urlencode(device_id)
    if since and tonumber(since) and tonumber(since) > 0 then
        path = path .. "&since=" .. string.format("%.3f", tonumber(since))
    end

    local ok, code, body = self:_request("GET", path, nil, nil, {
        block_timeout = 30,
        total_timeout = 90,
        attempts = 2,
    })
    if not ok then
        return false, body or ("HTTP " .. tostring(code))
    end

    local parsed, result = pcall(json.decode, body or "{}")
    if not parsed or type(result) ~= "table" then
        logger.warn("Bridge Sync API: Invalid merged statistics JSON")
        return false, "Invalid merged statistics response"
    end
    return true, result
end

return APIClient
