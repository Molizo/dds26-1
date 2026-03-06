"""Lua scripts for atomic order operations in Redis."""

# MARK_ORDER_PAID: Atomically check an order_paid flag and set it.
# Uses a separate order_paid:{order_id} key with SET NX semantics.
#
# KEYS[1] = order_paid:{order_id}
# Returns:
#   1  — flag was set (first call)
#   0  — flag already existed (idempotent no-op)
MARK_ORDER_PAID = """
local exists = redis.call('EXISTS', KEYS[1])
if exists == 1 then
    return 0
end
redis.call('SET', KEYS[1], '1')
return 1
"""
