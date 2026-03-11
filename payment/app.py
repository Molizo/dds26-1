import logging
import os
import atexit
import uuid

import redis
from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response

import service as payment_service

DB_ERROR_STR = "DB error"

app = Flask("payment-service")

db: redis.Redis = redis.Redis(
    host=os.environ['REDIS_HOST'],
    port=int(os.environ['REDIS_PORT']),
    password=os.environ['REDIS_PASSWORD'],
    db=int(os.environ['REDIS_DB']),
)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


class UserValue(Struct):
    credit: int


def get_user_from_db(user_id: str) -> UserValue:
    try:
        entry: bytes = db.get(user_id)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    entry: UserValue | None = msgpack.decode(entry, type=UserValue) if entry else None
    if entry is None:
        abort(400, f"User: {user_id} not found!")
    return entry


def _require_positive_int(value: int | str, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        abort(400, f"{field_name} must be a positive integer")
    if parsed <= 0:
        abort(400, f"{field_name} must be a positive integer")
    return parsed


@app.post('/create_user')
def create_user():
    key = str(uuid.uuid4())
    value = msgpack.encode(UserValue(credit=0))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'user_id': key})


@app.post('/batch_init/<n>/<starting_money>')
def batch_init_users(n: int, starting_money: int):
    n = int(n)
    starting_money = int(starting_money)
    kv_pairs: dict[str, bytes] = {
        f"{i}": msgpack.encode(UserValue(credit=starting_money))
        for i in range(n)
    }
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get('/find_user/<user_id>')
def find_user(user_id: str):
    user_entry: UserValue = get_user_from_db(user_id)
    return jsonify({"user_id": user_id, "credit": user_entry.credit})


@app.post('/add_funds/<user_id>/<amount>')
def add_credit(user_id: str, amount: int):
    amount = _require_positive_int(amount, "amount")
    result = payment_service.add_credit(db, user_id, amount)
    if not result["ok"]:
        error = result.get("error", "unknown")
        if error == "not_found":
            return abort(400, f"User: {user_id} not found!")
        return abort(400, f"Failed to add funds: {error}")
    return Response(f"User: {user_id} credit updated to: {result['credit']}", status=200)


@app.post('/pay/<user_id>/<amount>')
def remove_credit(user_id: str, amount: int):
    amount = _require_positive_int(amount, "amount")
    app.logger.debug("Removing %s credit from user: %s", amount, user_id)
    result = payment_service.pay_credit(db, user_id, amount)
    if not result["ok"]:
        error = result.get("error", "unknown")
        if error == "not_found":
            return abort(400, f"User: {user_id} not found!")
        if error == "insufficient_credit":
            return abort(400, f"User: {user_id} credit cannot get reduced below zero!")
        return abort(400, f"Failed to pay: {error}")
    return Response(f"User: {user_id} credit updated to: {result['credit']}", status=200)


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
