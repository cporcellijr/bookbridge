-- Serializes BridgeSync work and coalesces duplicate pending job families.

local Coordinator = {}
Coordinator.__index = Coordinator

Coordinator.PRIORITY = {
    automatic = 100,
    manual = 200,
    lifecycle = 300,
}

function Coordinator:new(now)
    return setmetatable({
        now = now or os.time,
        active = nil,
        pending = {},
        pending_count = 0,
        sequence = 0,
    }, self)
end

function Coordinator:isBusy()
    return self.active ~= nil
end

function Coordinator:_nextPending()
    local selected_family, selected
    for family, job in pairs(self.pending) do
        if not selected
            or job.priority > selected.priority
            or (job.priority == selected.priority and job.sequence < selected.sequence)
        then
            selected_family, selected = family, job
        end
    end
    return selected_family, selected
end

function Coordinator:_startNext()
    local family, job = self:_nextPending()
    if not job then return end
    self.pending[family] = nil
    self.pending_count = self.pending_count - 1
    self:_start(job)
end

function Coordinator:_start(job)
    self.active = job
    job.started_at = self.now()
    local finished = false
    local function done()
        if finished then return end
        finished = true
        if self.active == job then
            self.active = nil
            self:_startNext()
        end
    end
    local ok, err = pcall(job.run, done)
    if not ok then
        if job.on_error then pcall(job.on_error, err) end
        done()
    end
end

function Coordinator:submit(job)
    assert(type(job) == "table" and type(job.family) == "string" and job.family ~= "")
    assert(type(job.run) == "function")
    self.sequence = self.sequence + 1
    job.sequence = self.sequence
    job.created_at = self.now()
    job.priority = tonumber(job.priority) or Coordinator.PRIORITY.automatic

    if self.active then
        local current = self.pending[job.family]
        if not current then
            self.pending_count = self.pending_count + 1
            self.pending[job.family] = job
            return "queued"
        end
        if job.priority >= current.priority then
            self.pending[job.family] = job
            return "replaced"
        end
        return "kept"
    end

    self:_start(job)
    return "started"
end

local function statusFor(job, now)
    if not job then return nil end
    return {
        family = job.family,
        label = job.label or job.family,
        source = job.source,
        priority = job.priority,
        created_at = job.created_at,
        started_at = job.started_at,
        age = job.started_at and math.max(0, now - job.started_at) or nil,
    }
end

function Coordinator:status()
    local _, next_job = self:_nextPending()
    local now = self.now()
    return {
        current = statusFor(self.active, now),
        pending_count = self.pending_count,
        next = statusFor(next_job, now),
    }
end

return Coordinator
