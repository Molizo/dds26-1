"""Lua scripts for atomic stock operations.

All scripts run atomically on Redis with no interleaving.
StockValue items are stored as msgpack arrays: [stock, price] (index 1, 2 in Lua).
Participant tx records are stored as JSON under stock_tx:{tx_id}.

Public-endpoint scripts (no tx record):
  SUBTRACT_STOCK, ADD_STOCK

Transaction scripts (tx record for idempotency and recovery):
  STOCK_HOLD, STOCK_RELEASE, STOCK_COMMIT
"""

# ---------------------------------------------------------------------------
# Public endpoint scripts
# ---------------------------------------------------------------------------

# Atomically subtract stock from an item.
# KEYS[1] = item key
# ARGV[1] = amount (string representation of int)
# Returns: new stock level (integer) on success
# Errors: 'not_found', 'insufficient_stock'
SUBTRACT_STOCK = """
local data = redis.call('GET', KEYS[1])
if not data then
    return redis.error_reply('not_found')
end
local item = cmsgpack.unpack(data)
local amount = tonumber(ARGV[1])
if item["stock"] < amount then
    return redis.error_reply('insufficient_stock')
end
item["stock"] = item["stock"] - amount
redis.call('SET', KEYS[1], cmsgpack.pack(item))
return item["stock"]
"""

# Atomically add stock to an item.
# KEYS[1] = item key
# ARGV[1] = amount (string representation of int)
# Returns: new stock level (integer)
# Errors: 'not_found'
ADD_STOCK = """
local data = redis.call('GET', KEYS[1])
if not data then
    return redis.error_reply('not_found')
end
local item = cmsgpack.unpack(data)
item["stock"] = item["stock"] + tonumber(ARGV[1])
redis.call('SET', KEYS[1], cmsgpack.pack(item))
return item["stock"]
"""

# ---------------------------------------------------------------------------
# Transaction scripts
# ---------------------------------------------------------------------------

# Batch-atomic stock hold. All-or-nothing: if any item fails validation,
# nothing is modified.
#
# KEYS[1..n]  = item keys (one per unique item, in the same order as ARGV quantities)
# ARGV[1]     = tx_id
# ARGV[2]     = TTL seconds for the tx record
# ARGV[3..n+2] = quantities (one per item, corresponding to KEYS order)
#
# Returns JSON string: {"ok": true} or {"ok": false, "error": "...", "item": "..."}
#
# Idempotency: if a tx record already exists for tx_id, return the cached
# result immediately. The check must come first because stock levels may have
# changed since the original call — re-validating would produce spurious failures.
STOCK_HOLD = """
local tx_key = 'stock_tx:' .. ARGV[1]
local existing = redis.call('GET', tx_key)
if existing then
    return existing
end

local n = #KEYS

-- First pass: validate all items (fail-fast, no side effects)
for i = 1, n do
    local data = redis.call('GET', KEYS[i])
    if not data then
        return cjson.encode({ok=false, error='not_found', item=KEYS[i]})
    end
    local item = cmsgpack.unpack(data)
    local qty = tonumber(ARGV[i + 2])
    if item["stock"] < qty then
        return cjson.encode({ok=false, error='insufficient_stock', item=KEYS[i]})
    end
end

-- Second pass: subtract (only reached when all checks passed)
for i = 1, n do
    local data = redis.call('GET', KEYS[i])
    local item = cmsgpack.unpack(data)
    item["stock"] = item["stock"] - tonumber(ARGV[i + 2])
    redis.call('SET', KEYS[i], cmsgpack.pack(item))
end

-- Persist tx record with TTL so stale records don't accumulate
local result = cjson.encode({ok=true, status='held'})
redis.call('SET', tx_key, result, 'EX', tonumber(ARGV[2]))
return cjson.encode({ok=true})
"""

# Batch-atomic stock release. Adds back all quantities and marks tx released.
#
# KEYS[1..n]  = item keys (same order as the original hold command)
# ARGV[1]     = tx_id
# ARGV[2]     = TTL seconds for the tx record
# ARGV[3..n+2] = quantities to restore
#
# Returns JSON string: {"ok": true} or {"ok": false, "error": "..."}
#
# If the tx record is absent, the hold never happened (or was already cleaned
# up): return ok=true with error="tx_not_found" so the coordinator can treat
# it as a harmless no-op without masking real errors.
STOCK_RELEASE = """
local tx_key = 'stock_tx:' .. ARGV[1]
local tx_data = redis.call('GET', tx_key)

if not tx_data then
    return cjson.encode({ok=true, error='tx_not_found'})
end

local tx = cjson.decode(tx_data)

if tx.status == 'released' then
    return cjson.encode({ok=true})
end

if tx.status == 'committed' then
    return cjson.encode({ok=false, error='already_committed'})
end

-- Status is 'held' — restore all quantities
local n = #KEYS
for i = 1, n do
    local data = redis.call('GET', KEYS[i])
    if data then
        local item = cmsgpack.unpack(data)
        item["stock"] = item["stock"] + tonumber(ARGV[i + 2])
        redis.call('SET', KEYS[i], cmsgpack.pack(item))
    end
end

tx.status = 'released'
redis.call('SET', tx_key, cjson.encode(tx), 'EX', tonumber(ARGV[2]))
return cjson.encode({ok=true})
"""

# Mark a stock tx record as committed. No stock change — the subtraction
# already happened during hold. This gives the tx record a clean terminal state.
#
# KEYS[1]  = tx key (stock_tx:{tx_id})
# ARGV[1]  = tx_id
# ARGV[2]  = TTL seconds
#
# Returns JSON string: {"ok": true} or {"ok": false, "error": "..."}
STOCK_COMMIT = """
local tx_key = 'stock_tx:' .. ARGV[1]
local tx_data = redis.call('GET', tx_key)

if not tx_data then
    return cjson.encode({ok=true, error='tx_not_found'})
end

local tx = cjson.decode(tx_data)

if tx.status == 'committed' then
    return cjson.encode({ok=true})
end

if tx.status == 'released' then
    return cjson.encode({ok=false, error='already_released'})
end

tx.status = 'committed'
redis.call('SET', tx_key, cjson.encode(tx), 'EX', tonumber(ARGV[2]))
return cjson.encode({ok=true})
"""
