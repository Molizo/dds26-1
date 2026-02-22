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
from keys import tx_key, tx_lock_key, user_key
from lua_scripts import (
    ABORT_PAY_SCRIPT,
    COMMIT_PAY_SCRIPT,
    DIRECT_PAY_SCRIPT,
    PREPARE_PAY_SCRIPT,
    SAGA_PAY_SCRIPT,
    SAGA_REFUND_SCRIPT,
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


def get_user_credit(user_id: str) -> int:
    try:
        credit = db.hget(user_key(user_id), "credit")
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if credit is None:
        abort(400, f"User: {user_id} not found!")
    return int(credit)


def create_user_handler():
    import uuid  # local import keeps startup surface small
    key = str(uuid.uuid4())
    try:
        db.hset(user_key(key), mapping={"credit": 0})
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"user_id": key})


def batch_init_users_handler(n: int, starting_money: int):
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


def find_user_handler(user_id: str):
    return jsonify({"user_id": user_id, "credit": get_user_credit(user_id)})


def add_credit_handler(user_id: str, amount: int):
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


def remove_credit_handler(user_id: str, amount: int):
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


def _acquire_internal_tx_lock(tx_id: str) -> str:
    try:
        token = acquire_lock(db, tx_lock_key(tx_id), TX_LOCK_TTL_SECONDS)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if token is None:
        abort(409, f"Transaction already in progress for tx_id: {tx_id}")
    return token


def _release_internal_tx_lock(tx_id: str, token: str):
    try:
        release_lock(db, tx_lock_key(tx_id), token)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def internal_saga_pay_handler(tx_id: str, user_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)
    lock_token = _acquire_internal_tx_lock(tx_id)
    try:
        result = run_lua(
            SAGA_PAY_SCRIPT,
            [user_key(user_id), tx_key(tx_id)],
            [user_id, str(amount), str(now_ms())],
        )
    finally:
        _release_internal_tx_lock(tx_id, lock_token)

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


def internal_saga_refund_handler(tx_id: str):
    lock_token = _acquire_internal_tx_lock(tx_id)
    try:
        result = run_lua(SAGA_REFUND_SCRIPT, [tx_key(tx_id)], [str(now_ms())])
    finally:
        _release_internal_tx_lock(tx_id, lock_token)

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


def internal_prepare_pay_handler(tx_id: str, user_id: str, amount: int):
    validate_non_negative(amount)
    amount = int(amount)
    lock_token = _acquire_internal_tx_lock(tx_id)
    try:
        result = run_lua(
            PREPARE_PAY_SCRIPT,
            [user_key(user_id), tx_key(tx_id)],
            [user_id, str(amount), str(now_ms())],
        )
    finally:
        _release_internal_tx_lock(tx_id, lock_token)

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


def internal_commit_pay_handler(tx_id: str):
    lock_token = _acquire_internal_tx_lock(tx_id)
    try:
        result = run_lua(COMMIT_PAY_SCRIPT, [tx_key(tx_id)], [str(now_ms())])
    finally:
        _release_internal_tx_lock(tx_id, lock_token)

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


def internal_abort_pay_handler(tx_id: str):
    lock_token = _acquire_internal_tx_lock(tx_id)
    try:
        result = run_lua(ABORT_PAY_SCRIPT, [tx_key(tx_id)], [str(now_ms())])
    finally:
        _release_internal_tx_lock(tx_id, lock_token)

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
