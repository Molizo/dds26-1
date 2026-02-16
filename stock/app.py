import logging
import os
import atexit
import uuid

import redis

from flask import Flask, jsonify, abort, Response, request

from shared_messaging.redis_atomic import (
    ATOMIC_BUSINESS_REJECT,
    ATOMIC_MISSING_ENTITY,
    release_stock_atomic,
    reserve_stock_atomic,
)
from shared_messaging.redis_keys import stock_hash_key


DB_ERROR_STR = "DB error"

app = Flask("stock-service")

db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
                              port=int(os.environ['REDIS_PORT']),
                              password=os.environ['REDIS_PASSWORD'],
                              db=int(os.environ['REDIS_DB']),
                              decode_responses=True)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


def get_item_from_db(item_id: str) -> dict[str, int]:
    try:
        entry = db.hgetall(stock_hash_key(item_id))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    if not entry:
        abort(400, f"Item: {item_id} not found!")
    try:
        stock = int(entry["stock"])
        price = int(entry["price"])
    except (KeyError, ValueError, TypeError):
        abort(400, f"Item: {item_id} not found!")
    return {"stock": stock, "price": price}


def resolve_message_id(default_scope: str) -> str:
    idempotency_key = request.headers.get("X-Idempotency-Key")
    if idempotency_key:
        return f"{default_scope}:{idempotency_key}"
    return f"{default_scope}:{uuid.uuid4()}"


@app.post('/item/create/<price>')
def create_item(price: int):
    key = str(uuid.uuid4())
    app.logger.debug(f"Item: {key} created")
    try:
        db.hset(stock_hash_key(key), mapping={"stock": 0, "price": int(price)})
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'item_id': key})


@app.post('/batch_init/<n>/<starting_stock>/<item_price>')
def batch_init_users(n: int, starting_stock: int, item_price: int):
    n = int(n)
    starting_stock = int(starting_stock)
    item_price = int(item_price)
    try:
        with db.pipeline() as pipe:
            for i in range(n):
                pipe.hset(stock_hash_key(f"{i}"), mapping={"stock": starting_stock, "price": item_price})
            pipe.execute()
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for stock successful"})


@app.get('/find/<item_id>')
def find_item(item_id: str):
    item_entry = get_item_from_db(item_id)
    return jsonify(
        {
            "stock": item_entry["stock"],
            "price": item_entry["price"]
        }
    )


@app.post('/add/<item_id>/<amount>')
def add_stock(item_id: str, amount: int):
    amount = int(amount)
    message_id = resolve_message_id(f"rest-stock-add:{item_id}:{amount}")
    try:
        result = release_stock_atomic(
            db,
            service="stock-rest",
            message_id=message_id,
            item_id=item_id,
            step="rest_add",
            quantity=amount,
        )
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    if result.status == ATOMIC_MISSING_ENTITY:
        abort(400, f"Item: {item_id} not found!")
    if result.status == ATOMIC_BUSINESS_REJECT:
        abort(400, f"Item: {item_id} stock add amount is invalid!")

    item_entry = get_item_from_db(item_id)
    return Response(f"Item: {item_id} stock updated to: {item_entry['stock']}", status=200)


@app.post('/subtract/<item_id>/<amount>')
def remove_stock(item_id: str, amount: int):
    amount = int(amount)
    message_id = resolve_message_id(f"rest-stock-subtract:{item_id}:{amount}")
    try:
        result = reserve_stock_atomic(
            db,
            service="stock-rest",
            message_id=message_id,
            item_id=item_id,
            step="rest_subtract",
            quantity=amount,
        )
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)

    if result.status == ATOMIC_MISSING_ENTITY:
        abort(400, f"Item: {item_id} not found!")
    if result.status == ATOMIC_BUSINESS_REJECT:
        abort(400, f"Item: {item_id} stock cannot get reduced below zero!")

    item_entry = get_item_from_db(item_id)
    app.logger.debug(f"Item: {item_id} stock updated to: {item_entry['stock']}")
    return Response(f"Item: {item_id} stock updated to: {item_entry['stock']}", status=200)


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
