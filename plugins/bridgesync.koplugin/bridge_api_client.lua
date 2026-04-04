local socket = require("socket")
local http = require("socket.http")
local ltn12 = require("ltn12")
local json = require("json")
local logger = require("logger")
local socketutil = require("socketutil")

local KOSYNC_ACCEPT = "application/vnd.koreader.v1+json"

local APIClient = {
    server_url = "",
    username = "",
    key = "",
    timeout = 30,
    log_callback = nil,
}

function APIClient:new(o)
    o = o or {}
    setmetatable(o, self)
    self.__index = self
    return o
end

function APIClient:init(server_url, username, key, log_callback)
    self.server_url = tostring(server_url or ""):gsub("/+$", "")
    self.username = tostring(username or "")
    self.key = tostring(key or "")
    self.log_callback = log_callback
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
    local total_timeout = opts.total_timeout or (sink and 300 or 60)
    local attempts = opts.attempts or 1

    for attempt = 1, attempts do
        socketutil:set_timeout(block_timeout, total_timeout)

        local response_body = sink and nil or {}
        local request_sink = sink or socketutil.table_sink(response_body)
        local request = {
            url = url,
            method = method,
            headers = self:_build_headers(extra_headers),
            sink = request_sink,
        }
        local code, response_headers, status = socket.skip(1, http.request(request))
        socketutil:reset_timeout()

        if code == socketutil.TIMEOUT_CODE or
           code == socketutil.SSL_HANDSHAKE_CODE or
           code == socketutil.SINK_TIMEOUT_CODE
        then
            self:_log("warn", "Request interrupted:", tostring(code))
            if attempt < attempts then
                self:_log("info", "Retrying request", tostring(attempt + 1), "of", tostring(attempts))
                socket.sleep(attempt)
            else
                return false, nil, tostring(code)
            end
        else
            if response_headers == nil then
                self:_log("warn", "No HTTP headers:", tostring(status or code or "Connection failed"))
                return false, nil, tostring(status or code or "Connection failed")
            end
            if type(code) ~= "number" then
                self:_log("warn", "Non-numeric response code:", tostring(code))
                return false, nil, tostring(code)
            end

            local body = response_body and table.concat(response_body) or nil
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
    if ok then
        return true, "Authentication successful"
    end
    return false, "Auth failed: " .. tostring(code or body or "Unknown error")
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

function APIClient:downloadBook(download_path, save_path)
    local attempts = 3
    for attempt = 1, attempts do
        local handle, open_err = io.open(save_path, "wb")
        if not handle then
            return false, open_err or "Failed to open output file"
        end

        local ok, code, body = self:_request("GET", download_path, socketutil.file_sink(handle), nil, {
            block_timeout = 60,
            total_timeout = 300,
            attempts = 1,
        })
        if ok then
            return true
        end

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

    local url = self.server_url .. path
    self:_log("info", method, url)
    local opts = timeout_opts or {}
    local block_timeout = opts.block_timeout or self.timeout
    local total_timeout = opts.total_timeout or 60
    local attempts = opts.attempts or 1

    for attempt = 1, attempts do
        socketutil:set_timeout(block_timeout, total_timeout)

        local response_body = {}
        local headers = self:_build_headers({
            ["content-type"] = "application/json",
            ["content-length"] = tostring(#json_body),
        })
        local request = {
            url = url,
            method = method,
            headers = headers,
            source = ltn12.source.string(json_body),
            sink = socketutil.table_sink(response_body),
        }
        local code, response_headers, status = socket.skip(1, http.request(request))
        socketutil:reset_timeout()

        if code == socketutil.TIMEOUT_CODE or
           code == socketutil.SSL_HANDSHAKE_CODE or
           code == socketutil.SINK_TIMEOUT_CODE
        then
            self:_log("warn", "Request interrupted:", tostring(code))
            if attempt < attempts then
                self:_log("info", "Retrying request", tostring(attempt + 1), "of", tostring(attempts))
                socket.sleep(attempt)
            else
                return false, nil, tostring(code)
            end
        else
            if response_headers == nil then
                self:_log("warn", "No HTTP headers:", tostring(status or code or "Connection failed"))
                return false, nil, tostring(status or code or "Connection failed")
            end
            if type(code) ~= "number" then
                self:_log("warn", "Non-numeric response code:", tostring(code))
                return false, nil, tostring(code)
            end

            local body = table.concat(response_body)
            if code >= 200 and code < 300 then
                return true, code, body, response_headers, status
            end
            self:_log("warn", "HTTP failure:", tostring(code), tostring(body or status or ""))
            return false, code, body or status or ("HTTP " .. tostring(code)), response_headers, status
        end
    end

    return false, nil, "Request failed"
end

function APIClient:uploadSessions(sessions)
    local body = json.encode(sessions)
    return self:_requestJSON("POST", "/koreader/device-sync/sessions", body, {
        block_timeout = 20,
        total_timeout = 60,
        attempts = 2,
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

return APIClient
