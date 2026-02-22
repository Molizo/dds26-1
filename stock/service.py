import redis
from flask import Response, abort, jsonify

from common.redis_lock import acquire_lock, release_lock
from common.time_utils import now_ms
from config import (
    DB_ERROR_STR,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    TX_LOCK_TTL_SECONDS,
)
from keys import item_key, tx_key, tx_lock_key
from lua_scripts import (
    ABORT_SUBTRACT_SCRIPT,
    COMMIT_SUBTRACT_SCRIPT,
    DIRECT_SUBTRACT_SCRIPT,
    PREPARE_SUBTRACT_SCRIPT,
    SAGA_RELEASE_SCRIPT,
    SAGA_RESERVE_SCRIPT,
)

db: redis.Redis = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    db=REDIS_DB,
)


def close_db_connection():
    db.close()


def validate_non_negative(amount: int):
    if int(amount) < 0:
        abort(400, f"Amount cannot be negative, got {amount}")


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


def create_item_handler(price: int):
    import uuid  # local import to keep startup surface small

    key = str(uuid.uuid4())
    try:
        db.hset(item_key(key), mapping={"stock": 0, "price": int(price)})
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"item_id": key})


def batch_init_stock_handler(n: int, starting_stock: int, item_price: int):
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


def find_item_handler(item_id: str):
    return jsonify(get_item(item_id))


def add_stock_handler(item_id: str, amount: int):
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


def remove_stock_handler(item_id: str, amount: int):
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


def _acquire_internal_tx_lock(tx_id: str, item_id: str) -> str:
    try:
        token = acquire_lock(db, tx_lock_key(tx_id, item_id), TX_LOCK_TTL_SECONDS)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")
    return token


def _release_internal_tx_lock(tx_id: str, item_id: str, token: str):
    try:
        release_lock(db, tx_lock_key(tx_id, item_id), token)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def internal_saga_reserve_handler(tx_id: str, item_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)
    lock_token = _acquire_internal_tx_lock(tx_id, item_id)
    try:
        result = run_lua(
            SAGA_RESERVE_SCRIPT,
            [item_key(item_id), tx_key(tx_id, item_id)],
            [item_id, str(amount), str(now_ms())],
        )
    finally:
        _release_internal_tx_lock(tx_id, item_id, lock_token)

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


def internal_saga_release_handler(tx_id: str, item_id: str):
    lock_token = _acquire_internal_tx_lock(tx_id, item_id)
    try:
        result = run_lua(SAGA_RELEASE_SCRIPT, [tx_key(tx_id, item_id)], [str(now_ms())])
    finally:
        _release_internal_tx_lock(tx_id, item_id, lock_token)

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


def internal_prepare_subtract_handler(tx_id: str, item_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)
    lock_token = _acquire_internal_tx_lock(tx_id, item_id)
    try:
        result = run_lua(
            PREPARE_SUBTRACT_SCRIPT,
            [item_key(item_id), tx_key(tx_id, item_id)],
            [item_id, str(amount), str(now_ms())],
        )
    finally:
        _release_internal_tx_lock(tx_id, item_id, lock_token)

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


def internal_commit_subtract_handler(tx_id: str, item_id: str):
    lock_token = _acquire_internal_tx_lock(tx_id, item_id)
    try:
        result = run_lua(COMMIT_SUBTRACT_SCRIPT, [tx_key(tx_id, item_id)], [str(now_ms())])
    finally:
        _release_internal_tx_lock(tx_id, item_id, lock_token)

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


def internal_abort_subtract_handler(tx_id: str, item_id: str):
    lock_token = _acquire_internal_tx_lock(tx_id, item_id)
    try:
        result = run_lua(ABORT_SUBTRACT_SCRIPT, [tx_key(tx_id, item_id)], [str(now_ms())])
    finally:
        _release_internal_tx_lock(tx_id, item_id, lock_token)

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
