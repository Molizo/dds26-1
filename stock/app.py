import logging
import os
import atexit
import uuid

import redis
from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response

import service as stock_service

DB_ERROR_STR = "DB error"

app = Flask("stock-service")

db: redis.Redis = redis.Redis(
    host=os.environ['REDIS_HOST'],
    port=int(os.environ['REDIS_PORT']),
    password=os.environ['REDIS_PASSWORD'],
    db=int(os.environ['REDIS_DB']),
)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


class StockValue(Struct):
    stock: int
    price: int


def get_item_from_db(item_id: str) -> StockValue:
    try:
        entry: bytes = db.get(item_id)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    entry: StockValue | None = msgpack.decode(entry, type=StockValue) if entry else None
    if entry is None:
        abort(400, f"Item: {item_id} not found!")
    return entry


@app.post('/item/create/<price>')
def create_item(price: int):
    key = str(uuid.uuid4())
    app.logger.debug("Item: %s created", key)
    value = msgpack.encode(StockValue(stock=0, price=int(price)))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'item_id': key})


@app.post('/batch_init/<n>/<starting_stock>/<item_price>')
def batch_init_users(n: int, starting_stock: int, item_price: int):
    n = int(n)
    starting_stock = int(starting_stock)
    item_price = int(item_price)
    kv_pairs: dict[str, bytes] = {
        f"{i}": msgpack.encode(StockValue(stock=starting_stock, price=item_price))
        for i in range(n)
    }
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for stock successful"})


@app.get('/find/<item_id>')
def find_item(item_id: str):
    item_entry: StockValue = get_item_from_db(item_id)
    return jsonify({"stock": item_entry.stock, "price": item_entry.price})


@app.post('/add/<item_id>/<amount>')
def add_stock(item_id: str, amount: int):
    result = stock_service.add_stock(db, item_id, int(amount))
    if not result["ok"]:
        error = result.get("error", "unknown")
        if error == "not_found":
            return abort(400, f"Item: {item_id} not found!")
        return abort(400, f"Failed to add stock: {error}")
    return Response(f"Item: {item_id} stock updated to: {result['stock']}", status=200)


@app.post('/subtract/<item_id>/<amount>')
def remove_stock(item_id: str, amount: int):
    result = stock_service.subtract_stock(db, item_id, int(amount))
    if not result["ok"]:
        error = result.get("error", "unknown")
        if error == "not_found":
            return abort(400, f"Item: {item_id} not found!")
        if error == "insufficient_stock":
            return abort(400, f"Item: {item_id} stock cannot get reduced below zero!")
        return abort(400, f"Failed to subtract stock: {error}")
    app.logger.debug("Item: %s stock updated to: %s", item_id, result['stock'])
    return Response(f"Item: {item_id} stock updated to: {result['stock']}", status=200)


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
