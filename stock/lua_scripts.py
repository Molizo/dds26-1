DIRECT_SUBTRACT_SCRIPT = """
local stock = redis.call("HGET", KEYS[1], "stock")
if not stock then
    return "NO_ITEM"
end
local amount = tonumber(ARGV[1])
if tonumber(stock) < amount then
    return "INSUFFICIENT"
end
redis.call("HINCRBY", KEYS[1], "stock", -amount)
return "OK"
"""

SAGA_RESERVE_SCRIPT = """
local state = redis.call("HGET", KEYS[2], "state")
if state then
    local protocol = redis.call("HGET", KEYS[2], "protocol")
    local tx_item = redis.call("HGET", KEYS[2], "item_id")
    local tx_amount = redis.call("HGET", KEYS[2], "amount")
    if protocol ~= "saga" or tx_item ~= ARGV[1] or tx_amount ~= ARGV[2] then
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

local stock = redis.call("HGET", KEYS[1], "stock")
if not stock then
    return "NO_ITEM"
end
local amount = tonumber(ARGV[2])
if tonumber(stock) < amount then
    return "INSUFFICIENT"
end
redis.call("HINCRBY", KEYS[1], "stock", -amount)
redis.call(
    "HSET",
    KEYS[2],
    "protocol", "saga",
    "item_id", ARGV[1],
    "amount", ARGV[2],
    "state", "COMMITTED",
    "updated_at_ms", ARGV[3]
)
return "OK"
"""

SAGA_RELEASE_SCRIPT = """
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

local item_id = redis.call("HGET", KEYS[1], "item_id")
local amount = redis.call("HGET", KEYS[1], "amount")
local item_key = "item:" .. item_id
local stock = redis.call("HGET", item_key, "stock")
if not stock then
    return "NO_ITEM"
end
redis.call("HINCRBY", item_key, "stock", tonumber(amount))
redis.call("HSET", KEYS[1], "state", "ABORTED", "updated_at_ms", ARGV[1])
return "OK"
"""

PREPARE_SUBTRACT_SCRIPT = """
local state = redis.call("HGET", KEYS[2], "state")
if state then
    local protocol = redis.call("HGET", KEYS[2], "protocol")
    local tx_item = redis.call("HGET", KEYS[2], "item_id")
    local tx_amount = redis.call("HGET", KEYS[2], "amount")
    if protocol ~= "2pc" or tx_item ~= ARGV[1] or tx_amount ~= ARGV[2] then
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

local stock = redis.call("HGET", KEYS[1], "stock")
if not stock then
    return "NO_ITEM"
end
local amount = tonumber(ARGV[2])
if tonumber(stock) < amount then
    return "INSUFFICIENT"
end
redis.call("HINCRBY", KEYS[1], "stock", -amount)
redis.call(
    "HSET",
    KEYS[2],
    "protocol", "2pc",
    "item_id", ARGV[1],
    "amount", ARGV[2],
    "state", "PREPARED",
    "updated_at_ms", ARGV[3]
)
return "OK"
"""

COMMIT_SUBTRACT_SCRIPT = """
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

ABORT_SUBTRACT_SCRIPT = """
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

local item_id = redis.call("HGET", KEYS[1], "item_id")
local amount = redis.call("HGET", KEYS[1], "amount")
local item_key = "item:" .. item_id
local stock = redis.call("HGET", item_key, "stock")
if not stock then
    return "NO_ITEM"
end
redis.call("HINCRBY", item_key, "stock", tonumber(amount))
redis.call("HSET", KEYS[1], "state", "ABORTED", "updated_at_ms", ARGV[1])
return "OK"
"""

