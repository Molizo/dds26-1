import logging
import os
import atexit
import uuid

import redis

from flask import Flask, jsonify, abort, Response, request

from shared_messaging.redis_atomic import (
    ATOMIC_BUSINESS_REJECT,
    ATOMIC_MISSING_ENTITY,
    charge_payment_atomic,
    refund_payment_atomic,
)
from shared_messaging.redis_keys import payment_hash_key

DB_ERROR_STR = "DB error"


app = Flask("payment-service")

db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
                              port=int(os.environ['REDIS_PORT']),
                              password=os.environ['REDIS_PASSWORD'],
                              db=int(os.environ['REDIS_DB']),
                              decode_responses=True)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


def get_user_from_db(user_id: str) -> dict[str, int]:
    try:
        entry = db.hgetall(payment_hash_key(user_id))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    if not entry:
        abort(400, f"User: {user_id} not found!")
    try:
        credit = int(entry["credit"])
    except (KeyError, ValueError, TypeError):
        abort(400, f"User: {user_id} not found!")
    return {"credit": credit}


def resolve_message_id(default_scope: str) -> str:
    idempotency_key = request.headers.get("X-Idempotency-Key")
    if idempotency_key:
        return f"{default_scope}:{idempotency_key}"
    return f"{default_scope}:{uuid.uuid4()}"


@app.post('/create_user')
def create_user():
    key = str(uuid.uuid4())
    try:
        db.hset(payment_hash_key(key), mapping={"credit": 0})
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'user_id': key})


@app.post('/batch_init/<n>/<starting_money>')
def batch_init_users(n: int, starting_money: int):
    n = int(n)
    starting_money = int(starting_money)
    try:
        with db.pipeline() as pipe:
            for i in range(n):
                pipe.hset(payment_hash_key(f"{i}"), mapping={"credit": starting_money})
            pipe.execute()
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get('/find_user/<user_id>')
def find_user(user_id: str):
    user_entry = get_user_from_db(user_id)
    return jsonify(
        {
            "user_id": user_id,
            "credit": user_entry["credit"]
        }
    )


@app.post('/add_funds/<user_id>/<amount>')
def add_credit(user_id: str, amount: int):
    amount = int(amount)
    message_id = resolve_message_id(f"rest-payment-add:{user_id}:{amount}")
    try:
        result = refund_payment_atomic(
            db,
            service="payment-rest",
            message_id=message_id,
            user_id=user_id,
            step="rest_add_funds",
            amount=amount,
        )
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    if result.status == ATOMIC_MISSING_ENTITY:
        abort(400, f"User: {user_id} not found!")
    if result.status == ATOMIC_BUSINESS_REJECT:
        abort(400, f"User: {user_id} credit add amount is invalid!")

    user_entry = get_user_from_db(user_id)
    return Response(f"User: {user_id} credit updated to: {user_entry['credit']}", status=200)


@app.post('/pay/<user_id>/<amount>')
def remove_credit(user_id: str, amount: int):
    app.logger.debug(f"Removing {amount} credit from user: {user_id}")
    amount = int(amount)
    message_id = resolve_message_id(f"rest-payment-pay:{user_id}:{amount}")
    try:
        result = charge_payment_atomic(
            db,
            service="payment-rest",
            message_id=message_id,
            user_id=user_id,
            step="rest_pay",
            amount=amount,
        )
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)

    if result.status == ATOMIC_MISSING_ENTITY:
        abort(400, f"User: {user_id} not found!")
    if result.status == ATOMIC_BUSINESS_REJECT:
        abort(400, f"User: {user_id} credit cannot get reduced below zero!")

    user_entry = get_user_from_db(user_id)
    return Response(f"User: {user_id} credit updated to: {user_entry['credit']}", status=200)


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
