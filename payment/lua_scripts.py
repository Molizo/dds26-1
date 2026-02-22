DIRECT_PAY_SCRIPT = """
local credit = redis.call("HGET", KEYS[1], "credit")
if not credit then
    return "NO_USER"
end
local amount = tonumber(ARGV[1])
if tonumber(credit) < amount then
    return "INSUFFICIENT"
end
redis.call("HINCRBY", KEYS[1], "credit", -amount)
return "OK"
"""

SAGA_PAY_SCRIPT = """
local state = redis.call("HGET", KEYS[2], "state")
if state then
    local protocol = redis.call("HGET", KEYS[2], "protocol")
    local tx_user = redis.call("HGET", KEYS[2], "user_id")
    local tx_amount = redis.call("HGET", KEYS[2], "amount")
    if protocol ~= "saga" or tx_user ~= ARGV[1] or tx_amount ~= ARGV[2] then
        return "MISMATCH"
    end
    if state == "COMMITTED" then
        return "ALREADY_COMMITTED"
    end
    if state == "ABORTED" then
        return "ALREADY_ABORTED"
    end
    return "INVALID_STATE"
end

local credit = redis.call("HGET", KEYS[1], "credit")
if not credit then
    return "NO_USER"
end
local amount = tonumber(ARGV[2])
if tonumber(credit) < amount then
    return "INSUFFICIENT"
end
redis.call("HINCRBY", KEYS[1], "credit", -amount)
redis.call(
    "HSET",
    KEYS[2],
    "protocol", "saga",
    "user_id", ARGV[1],
    "amount", ARGV[2],
    "state", "COMMITTED",
    "updated_at_ms", ARGV[3]
)
return "OK"
"""

SAGA_REFUND_SCRIPT = """
local state = redis.call("HGET", KEYS[1], "state")
if not state then
    return "NO_TX"
end
local protocol = redis.call("HGET", KEYS[1], "protocol")
if protocol ~= "saga" then
    return "MISMATCH"
end
if state == "ABORTED" then
    return "ALREADY_ABORTED"
end
if state ~= "COMMITTED" then
    return "INVALID_STATE"
end

local user_id = redis.call("HGET", KEYS[1], "user_id")
local amount = redis.call("HGET", KEYS[1], "amount")
local user_key = "user:" .. user_id
local credit = redis.call("HGET", user_key, "credit")
if not credit then
    return "NO_USER"
end
redis.call("HINCRBY", user_key, "credit", tonumber(amount))
redis.call("HSET", KEYS[1], "state", "ABORTED", "updated_at_ms", ARGV[1])
return "OK"
"""

PREPARE_PAY_SCRIPT = """
local state = redis.call("HGET", KEYS[2], "state")
if state then
    local protocol = redis.call("HGET", KEYS[2], "protocol")
    local tx_user = redis.call("HGET", KEYS[2], "user_id")
    local tx_amount = redis.call("HGET", KEYS[2], "amount")
    if protocol ~= "2pc" or tx_user ~= ARGV[1] or tx_amount ~= ARGV[2] then
        return "MISMATCH"
    end
    if state == "PREPARED" or state == "COMMITTED" then
        return "ALREADY_PREPARED"
    end
    if state == "ABORTED" then
        return "ALREADY_ABORTED"
    end
    return "INVALID_STATE"
end

local credit = redis.call("HGET", KEYS[1], "credit")
if not credit then
    return "NO_USER"
end
local amount = tonumber(ARGV[2])
if tonumber(credit) < amount then
    return "INSUFFICIENT"
end
redis.call("HINCRBY", KEYS[1], "credit", -amount)
redis.call(
    "HSET",
    KEYS[2],
    "protocol", "2pc",
    "user_id", ARGV[1],
    "amount", ARGV[2],
    "state", "PREPARED",
    "updated_at_ms", ARGV[3]
)
return "OK"
"""

COMMIT_PAY_SCRIPT = """
local state = redis.call("HGET", KEYS[1], "state")
if not state then
    return "NO_TX"
end
local protocol = redis.call("HGET", KEYS[1], "protocol")
if protocol ~= "2pc" then
    return "MISMATCH"
end
if state == "COMMITTED" then
    return "ALREADY_COMMITTED"
end
if state == "ABORTED" then
    return "ALREADY_ABORTED"
end
if state ~= "PREPARED" then
    return "INVALID_STATE"
end
redis.call("HSET", KEYS[1], "state", "COMMITTED", "updated_at_ms", ARGV[1])
return "OK"
"""

ABORT_PAY_SCRIPT = """
local state = redis.call("HGET", KEYS[1], "state")
if not state then
    return "NO_TX_NOOP"
end
local protocol = redis.call("HGET", KEYS[1], "protocol")
if protocol ~= "2pc" then
    return "MISMATCH"
end
if state == "ABORTED" then
    return "ALREADY_ABORTED"
end
if state == "COMMITTED" then
    return "ALREADY_COMMITTED"
end
if state ~= "PREPARED" then
    return "INVALID_STATE"
end

local user_id = redis.call("HGET", KEYS[1], "user_id")
local amount = redis.call("HGET", KEYS[1], "amount")
local user_key = "user:" .. user_id
local credit = redis.call("HGET", user_key, "credit")
if not credit then
    return "NO_USER"
end
redis.call("HINCRBY", user_key, "credit", tonumber(amount))
redis.call("HSET", KEYS[1], "state", "ABORTED", "updated_at_ms", ARGV[1])
return "OK"
"""

