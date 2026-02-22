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
USER_KEY_PREFIX = "user:"
TX_KEY_PREFIX = "ptx:"
TX_LOCK_KEY_PREFIX = "internal_tx_lock:"
TX_LOCK_TTL_SECONDS = int(os.environ.get("TX_LOCK_TTL_SECONDS", "10"))

LOCK_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

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

app = Flask("payment-service")

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


def user_key(user_id: str) -> str:
    return f"{USER_KEY_PREFIX}{user_id}"


def tx_key(tx_id: str) -> str:
    return f"{TX_KEY_PREFIX}{tx_id}"


def tx_lock_key(tx_id: str) -> str:
    return f"{TX_LOCK_KEY_PREFIX}{tx_id}"


def validate_non_negative(amount: int):
    if int(amount) < 0:
        abort(400, f"Amount cannot be negative, got {amount}")


def acquire_tx_lock(tx_id: str) -> str | None:
    token = str(uuid.uuid4())
    try:
        acquired = db.set(tx_lock_key(tx_id), token, nx=True, ex=TX_LOCK_TTL_SECONDS)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if not acquired:
        return None
    return token


def release_tx_lock(tx_id: str, token: str):
    try:
        db.eval(LOCK_RELEASE_SCRIPT, 1, tx_lock_key(tx_id), token)
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


def get_user_credit(user_id: str) -> int:
    try:
        credit = db.hget(user_key(user_id), "credit")
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if credit is None:
        abort(400, f"User: {user_id} not found!")
    return int(credit)


@app.post("/create_user")
def create_user():
    key = str(uuid.uuid4())
    try:
        db.hset(user_key(key), mapping={"credit": 0})
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"user_id": key})


@app.post("/batch_init/<n>/<starting_money>")
def batch_init_users(n: int, starting_money: int):
    n = int(n)
    starting_money = int(starting_money)
    try:
        pipeline = db.pipeline()
        for i in range(n):
            pipeline.hset(user_key(f"{i}"), mapping={"credit": starting_money})
        pipeline.execute()
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get("/find_user/<user_id>")
def find_user(user_id: str):
    return jsonify({"user_id": user_id, "credit": get_user_credit(user_id)})


@app.post("/add_funds/<user_id>/<amount>")
def add_credit(user_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)

    try:
        if not db.exists(user_key(user_id)):
            abort(400, f"User: {user_id} not found!")
        db.hincrby(user_key(user_id), "credit", amount)
        updated_credit = int(db.hget(user_key(user_id), "credit"))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)

    return Response(f"User: {user_id} credit updated to: {updated_credit}", status=200)


@app.post("/pay/<user_id>/<amount>")
def remove_credit(user_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)

    result = run_lua(DIRECT_PAY_SCRIPT, [user_key(user_id)], [str(amount)])
    if result == "OK":
        updated_credit = get_user_credit(user_id)
        return Response(f"User: {user_id} credit updated to: {updated_credit}", status=200)
    if result == "NO_USER":
        abort(400, f"User: {user_id} not found!")
    if result == "INSUFFICIENT":
        abort(400, f"User: {user_id} credit cannot get reduced below zero!")
    abort(400, DB_ERROR_STR)


@app.post("/internal/tx/saga/pay/<tx_id>/<user_id>/<amount>")
def internal_saga_pay(tx_id: str, user_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)
    lock_token = acquire_tx_lock(tx_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(
            SAGA_PAY_SCRIPT,
            [user_key(user_id), tx_key(tx_id)],
            [user_id, str(amount), str(now_ms())],
        )
    finally:
        release_tx_lock(tx_id, lock_token)

    if result == "OK":
        return Response("Saga pay applied", status=200)
    if result == "ALREADY_COMMITTED":
        return Response("Saga pay already applied", status=200)
    if result == "ALREADY_ABORTED":
        abort(400, f"Saga pay tx already refunded for tx_id: {tx_id}")
    if result == "NO_USER":
        abort(400, f"User: {user_id} not found!")
    if result == "INSUFFICIENT":
        abort(400, "User out of credit")
    if result == "MISMATCH":
        abort(400, f"Transaction payload mismatch for tx_id: {tx_id}")
    abort(400, f"Saga pay invalid state for tx_id: {tx_id}")


@app.post("/internal/tx/saga/refund/<tx_id>")
def internal_saga_refund(tx_id: str):
    lock_token = acquire_tx_lock(tx_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(SAGA_REFUND_SCRIPT, [tx_key(tx_id)], [str(now_ms())])
    finally:
        release_tx_lock(tx_id, lock_token)

    if result == "OK":
        return Response("Saga refund applied", status=200)
    if result == "NO_TX":
        return Response("Saga refund no-op", status=200)
    if result == "ALREADY_ABORTED":
        return Response("Saga refund already applied", status=200)
    if result == "NO_USER":
        abort(400, f"Saga refund user missing for tx_id: {tx_id}")
    if result == "MISMATCH":
        abort(400, f"Transaction protocol mismatch for tx_id: {tx_id}")
    abort(400, f"Saga refund invalid state for tx_id: {tx_id}")


@app.post("/internal/tx/prepare_pay/<tx_id>/<user_id>/<amount>")
def internal_prepare_pay(tx_id: str, user_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)
    lock_token = acquire_tx_lock(tx_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(
            PREPARE_PAY_SCRIPT,
            [user_key(user_id), tx_key(tx_id)],
            [user_id, str(amount), str(now_ms())],
        )
    finally:
        release_tx_lock(tx_id, lock_token)

    if result == "OK":
        return Response("2PC pay prepared", status=200)
    if result == "ALREADY_PREPARED":
        return Response("2PC pay already prepared", status=200)
    if result == "ALREADY_ABORTED":
        abort(400, f"2PC pay tx already aborted for tx_id: {tx_id}")
    if result == "NO_USER":
        abort(400, f"User: {user_id} not found!")
    if result == "INSUFFICIENT":
        abort(400, "User out of credit")
    if result == "MISMATCH":
        abort(400, f"Transaction payload mismatch for tx_id: {tx_id}")
    abort(400, f"2PC pay invalid state for tx_id: {tx_id}")


@app.post("/internal/tx/commit_pay/<tx_id>")
def internal_commit_pay(tx_id: str):
    lock_token = acquire_tx_lock(tx_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(COMMIT_PAY_SCRIPT, [tx_key(tx_id)], [str(now_ms())])
    finally:
        release_tx_lock(tx_id, lock_token)

    if result == "OK":
        return Response("2PC pay committed", status=200)
    if result == "ALREADY_COMMITTED":
        return Response("2PC pay already committed", status=200)
    if result == "NO_TX":
        abort(400, f"2PC pay tx not found for tx_id: {tx_id}")
    if result == "ALREADY_ABORTED":
        abort(400, f"2PC pay tx already aborted for tx_id: {tx_id}")
    if result == "MISMATCH":
        abort(400, f"Transaction protocol mismatch for tx_id: {tx_id}")
    abort(400, f"2PC pay invalid state for tx_id: {tx_id}")


@app.post("/internal/tx/abort_pay/<tx_id>")
def internal_abort_pay(tx_id: str):
    lock_token = acquire_tx_lock(tx_id)
    if lock_token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")

    try:
        result = run_lua(ABORT_PAY_SCRIPT, [tx_key(tx_id)], [str(now_ms())])
    finally:
        release_tx_lock(tx_id, lock_token)

    if result == "OK":
        return Response("2PC pay aborted", status=200)
    if result == "NO_TX_NOOP":
        return Response("2PC pay abort no-op", status=200)
    if result == "ALREADY_ABORTED":
        return Response("2PC pay already aborted", status=200)
    if result == "ALREADY_COMMITTED":
        abort(400, f"2PC pay tx already committed for tx_id: {tx_id}")
    if result == "NO_USER":
        abort(400, f"2PC pay user missing for tx_id: {tx_id}")
    if result == "MISMATCH":
        abort(400, f"Transaction protocol mismatch for tx_id: {tx_id}")
    abort(400, f"2PC pay invalid state for tx_id: {tx_id}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
