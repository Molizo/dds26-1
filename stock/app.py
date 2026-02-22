import atexit
import logging
import os
import time
import uuid

import redis
from flask import Flask, Response, abort, jsonify

DB_ERROR_STR = "DB error"
STATE_PREPARED = "PREPARED"
STATE_COMMITTED = "COMMITTED"
STATE_ABORTED = "ABORTED"
ITEM_KEY_PREFIX = "item:"
TX_KEY_PREFIX = "stx:"
TX_LOCK_KEY_PREFIX = "internal_tx_lock:"
TX_LOCK_TTL_SECONDS = int(os.environ.get("TX_LOCK_TTL_SECONDS", "10"))

LOCK_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

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

app = Flask("stock-service")

db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


def now_ms() -> int:
    return int(time.time() * 1000)


def item_key(item_id: str) -> str:
    return f"{ITEM_KEY_PREFIX}{item_id}"


def tx_key(tx_id: str, item_id: str) -> str:
    return f"{TX_KEY_PREFIX}{tx_id}:{item_id}"


def tx_lock_key(tx_id: str, item_id: str) -> str:
    return f"{TX_LOCK_KEY_PREFIX}{tx_id}:{item_id}"


def validate_non_negative(amount: int):
    if int(amount) < 0:
        abort(400, f"Amount cannot be negative, got {amount}")


def acquire_tx_lock(tx_id: str, item_id: str) -> str | None:
    token = str(uuid.uuid4())
    try:
        acquired = db.set(tx_lock_key(tx_id, item_id), token, nx=True, ex=TX_LOCK_TTL_SECONDS)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if not acquired:
        return None
    return token


def release_tx_lock(tx_id: str, item_id: str, token: str):
    try:
        db.eval(LOCK_RELEASE_SCRIPT, 1, tx_lock_key(tx_id, item_id), token)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def run_lua(script: str, keys: list[str], args: list[str]) -> str:
    try:
        result = db.eval(script, len(keys), *keys, *args)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if isinstance(result, bytes):
        return result.decode()
    return str(result)


def get_item(item_id: str) -> dict[str, int]:
    try:
        fields = db.hgetall(item_key(item_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if not fields:
        abort(400, f"Item: {item_id} not found!")
    return {"stock": int(fields[b"stock"]), "price": int(fields[b"price"])}


@app.post("/item/create/<price>")
def create_item(price: int):
    key = str(uuid.uuid4())
    try:
        db.hset(item_key(key), mapping={"stock": 0, "price": int(price)})
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"item_id": key})


@app.post("/batch_init/<n>/<starting_stock>/<item_price>")
def batch_init_users(n: int, starting_stock: int, item_price: int):
    n = int(n)
    starting_stock = int(starting_stock)
    item_price = int(item_price)
    try:
        pipeline = db.pipeline()
        for i in range(n):
            pipeline.hset(item_key(f"{i}"), mapping={"stock": starting_stock, "price": item_price})
        pipeline.execute()
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for stock successful"})


@app.get("/find/<item_id>")
def find_item(item_id: str):
    return jsonify(get_item(item_id))


@app.post("/add/<item_id>/<amount>")
def add_stock(item_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)

    try:
        if not db.exists(item_key(item_id)):
            abort(400, f"Item: {item_id} not found!")
        db.hincrby(item_key(item_id), "stock", amount)
        updated_stock = int(db.hget(item_key(item_id), "stock"))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)

    return Response(f"Item: {item_id} stock updated to: {updated_stock}", status=200)


@app.post("/subtract/<item_id>/<amount>")
def remove_stock(item_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)

    result = run_lua(DIRECT_SUBTRACT_SCRIPT, [item_key(item_id)], [str(amount)])
    if result == "OK":
        return Response(f"Item: {item_id} stock updated", status=200)
    if result == "NO_ITEM":
        abort(400, f"Item: {item_id} not found!")
    if result == "INSUFFICIENT":
        abort(400, f"Item: {item_id} stock cannot get reduced below zero!")
    abort(400, DB_ERROR_STR)


@app.post("/internal/tx/saga/reserve/<tx_id>/<item_id>/<amount>")
def internal_saga_reserve(tx_id: str, item_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)
    lock_token = acquire_tx_lock(tx_id, item_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(
            SAGA_RESERVE_SCRIPT,
            [item_key(item_id), tx_key(tx_id, item_id)],
            [item_id, str(amount), str(now_ms())],
        )
    finally:
        release_tx_lock(tx_id, item_id, lock_token)

    if result == "OK":
        return Response("Saga stock reserve applied", status=200)
    if result == "ALREADY_COMMITTED":
        return Response("Saga stock reserve already applied", status=200)
    if result == "ALREADY_ABORTED":
        abort(400, f"Saga stock tx already released for tx_id: {tx_id}")
    if result == "NO_ITEM":
        abort(400, f"Item: {item_id} not found!")
    if result == "INSUFFICIENT":
        abort(400, f"Out of stock on item_id: {item_id}")
    if result == "MISMATCH":
        abort(400, f"Transaction payload mismatch for tx_id: {tx_id}")
    abort(400, f"Saga stock reserve invalid state for tx_id: {tx_id}")


@app.post("/internal/tx/saga/release/<tx_id>/<item_id>")
def internal_saga_release(tx_id: str, item_id: str):
    lock_token = acquire_tx_lock(tx_id, item_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(SAGA_RELEASE_SCRIPT, [tx_key(tx_id, item_id)], [str(now_ms())])
    finally:
        release_tx_lock(tx_id, item_id, lock_token)

    if result == "OK":
        return Response("Saga stock release applied", status=200)
    if result == "NO_TX":
        return Response("Saga stock release no-op", status=200)
    if result == "ALREADY_ABORTED":
        return Response("Saga stock release already applied", status=200)
    if result == "NO_ITEM":
        abort(400, f"Saga stock release missing item for tx_id: {tx_id}")
    if result == "MISMATCH":
        abort(400, f"Transaction protocol mismatch for tx_id: {tx_id}")
    abort(400, f"Saga stock release invalid state for tx_id: {tx_id}")


@app.post("/internal/tx/prepare_subtract/<tx_id>/<item_id>/<amount>")
def internal_prepare_subtract(tx_id: str, item_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)
    lock_token = acquire_tx_lock(tx_id, item_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(
            PREPARE_SUBTRACT_SCRIPT,
            [item_key(item_id), tx_key(tx_id, item_id)],
            [item_id, str(amount), str(now_ms())],
        )
    finally:
        release_tx_lock(tx_id, item_id, lock_token)

    if result == "OK":
        return Response("2PC stock prepared", status=200)
    if result == "ALREADY_PREPARED":
        return Response("2PC stock already prepared", status=200)
    if result == "ALREADY_ABORTED":
        abort(400, f"2PC stock tx already aborted for tx_id: {tx_id}")
    if result == "NO_ITEM":
        abort(400, f"Item: {item_id} not found!")
    if result == "INSUFFICIENT":
        abort(400, f"Out of stock on item_id: {item_id}")
    if result == "MISMATCH":
        abort(400, f"Transaction payload mismatch for tx_id: {tx_id}")
    abort(400, f"2PC stock invalid state for tx_id: {tx_id}")


@app.post("/internal/tx/commit_subtract/<tx_id>/<item_id>")
def internal_commit_subtract(tx_id: str, item_id: str):
    lock_token = acquire_tx_lock(tx_id, item_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(COMMIT_SUBTRACT_SCRIPT, [tx_key(tx_id, item_id)], [str(now_ms())])
    finally:
        release_tx_lock(tx_id, item_id, lock_token)

    if result == "OK":
        return Response("2PC stock committed", status=200)
    if result == "ALREADY_COMMITTED":
        return Response("2PC stock already committed", status=200)
    if result == "NO_TX":
        abort(400, f"2PC stock tx not found for tx_id: {tx_id}")
    if result == "ALREADY_ABORTED":
        abort(400, f"2PC stock tx already aborted for tx_id: {tx_id}")
    if result == "MISMATCH":
        abort(400, f"Transaction protocol mismatch for tx_id: {tx_id}")
    abort(400, f"2PC stock invalid state for tx_id: {tx_id}")


@app.post("/internal/tx/abort_subtract/<tx_id>/<item_id>")
def internal_abort_subtract(tx_id: str, item_id: str):
    lock_token = acquire_tx_lock(tx_id, item_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(ABORT_SUBTRACT_SCRIPT, [tx_key(tx_id, item_id)], [str(now_ms())])
    finally:
        release_tx_lock(tx_id, item_id, lock_token)

    if result == "OK":
        return Response("2PC stock aborted", status=200)
    if result == "NO_TX_NOOP":
        return Response("2PC stock abort no-op", status=200)
    if result == "ALREADY_ABORTED":
        return Response("2PC stock already aborted", status=200)
    if result == "ALREADY_COMMITTED":
        abort(400, f"2PC stock tx already committed for tx_id: {tx_id}")
    if result == "NO_ITEM":
        abort(400, f"2PC stock missing item for tx_id: {tx_id}")
    if result == "MISMATCH":
        abort(400, f"Transaction protocol mismatch for tx_id: {tx_id}")
    abort(400, f"2PC stock invalid state for tx_id: {tx_id}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
