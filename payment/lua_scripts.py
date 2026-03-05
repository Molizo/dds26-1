"""Lua scripts for atomic payment operations.

UserValue items are stored as msgpack arrays: [credit] (index 1 in Lua).
Participant tx records are stored as JSON under payment_tx:{tx_id}.

Public-endpoint scripts (no tx record):
  PAY_CREDIT, ADD_CREDIT

Transaction scripts (tx record for idempotency and recovery):
  PAYMENT_HOLD, PAYMENT_RELEASE, PAYMENT_COMMIT
"""

# ---------------------------------------------------------------------------
# Public endpoint scripts
# ---------------------------------------------------------------------------

# Atomically subtract credit from a user (payment).
# KEYS[1] = user key
# ARGV[1] = amount (string)
# Returns: new credit level (integer)
# Errors: 'not_found', 'insufficient_credit'
PAY_CREDIT = """
local data = redis.call('GET', KEYS[1])
if not data then
    return redis.error_reply('not_found')
end
local user = cmsgpack.unpack(data)
local amount = tonumber(ARGV[1])
if user["credit"] < amount then
    return redis.error_reply('insufficient_credit')
end
user["credit"] = user["credit"] - amount
redis.call('SET', KEYS[1], cmsgpack.pack(user))
return user["credit"]
"""

# Atomically add credit to a user.
# KEYS[1] = user key
# ARGV[1] = amount (string)
# Returns: new credit level (integer)
# Errors: 'not_found'
ADD_CREDIT = """
local data = redis.call('GET', KEYS[1])
if not data then
    return redis.error_reply('not_found')
end
local user = cmsgpack.unpack(data)
user["credit"] = user["credit"] + tonumber(ARGV[1])
redis.call('SET', KEYS[1], cmsgpack.pack(user))
return user["credit"]
"""

# ---------------------------------------------------------------------------
# Transaction scripts
# ---------------------------------------------------------------------------

# Atomically hold payment (subtract credit with tx record).
#
# KEYS[1] = user key
# ARGV[1] = tx_id
# ARGV[2] = TTL seconds for the tx record
# ARGV[3] = amount (string)
#
# Returns JSON string: {"ok": true} or {"ok": false, "error": "..."}
PAYMENT_HOLD = """
local tx_key = 'payment_tx:' .. ARGV[1]
local existing = redis.call('GET', tx_key)
if existing then
    return existing
end

local data = redis.call('GET', KEYS[1])
if not data then
    return cjson.encode({ok=false, error='not_found'})
end

local user = cmsgpack.unpack(data)
local amount = tonumber(ARGV[3])

if user["credit"] < amount then
    return cjson.encode({ok=false, error='insufficient_credit'})
end

user["credit"] = user["credit"] - amount
redis.call('SET', KEYS[1], cmsgpack.pack(user))

local result = cjson.encode({ok=true, status='held', amount=amount})
redis.call('SET', tx_key, result, 'EX', tonumber(ARGV[2]))
return cjson.encode({ok=true})
"""

# Atomically release a payment hold (refund credit).
#
# KEYS[1] = user key
# ARGV[1] = tx_id
# ARGV[2] = TTL seconds
# ARGV[3] = amount to restore (string)
#
# Returns JSON string: {"ok": true} or {"ok": false, "error": "..."}
PAYMENT_RELEASE = """
local tx_key = 'payment_tx:' .. ARGV[1]
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

-- Status is 'held' — restore credit
local data = redis.call('GET', KEYS[1])
if data then
    local user = cmsgpack.unpack(data)
    user["credit"] = user["credit"] + tonumber(ARGV[3])
    redis.call('SET', KEYS[1], cmsgpack.pack(user))
end

tx.status = 'released'
redis.call('SET', tx_key, cjson.encode(tx), 'EX', tonumber(ARGV[2]))
return cjson.encode({ok=true})
"""

# Mark a payment tx record as committed. No credit change — the subtraction
# already happened during hold.
#
# KEYS[1] = tx key (payment_tx:{tx_id})
# ARGV[1] = tx_id
# ARGV[2] = TTL seconds
#
# Returns JSON string: {"ok": true} or {"ok": false, "error": "..."}
PAYMENT_COMMIT = """
local tx_key = 'payment_tx:' .. ARGV[1]
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
