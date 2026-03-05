import logging
import os
import atexit
import random
import uuid
from collections import defaultdict

import redis
import requests
from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response

from store import (
    OrderValue,
    get_order,
    mark_order_paid,
    get_decision,
)
from common.constants import VALID_PROTOCOLS

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

CHECKOUT_PROTOCOL = os.environ.get('CHECKOUT_PROTOCOL', '').lower()
if CHECKOUT_PROTOCOL not in VALID_PROTOCOLS:
    raise RuntimeError(
        f"CHECKOUT_PROTOCOL must be one of {sorted(VALID_PROTOCOLS)}, "
        f"got: {CHECKOUT_PROTOCOL!r}"
    )

STOCK_SERVICE_URL = os.environ['STOCK_SERVICE_URL']
PAYMENT_SERVICE_URL = os.environ['PAYMENT_SERVICE_URL']

app = Flask("order-service")

db: redis.Redis = redis.Redis(
    host=os.environ['REDIS_HOST'],
    port=int(os.environ['REDIS_PORT']),
    password=os.environ['REDIS_PASSWORD'],
    db=int(os.environ['REDIS_DB']),
)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)

# Reuse a single Session for connection pooling on outbound HTTP calls
_session = requests.Session()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_order_or_abort(order_id: str) -> OrderValue:
    order = get_order(db, order_id)
    if order is None:
        abort(400, f"Order: {order_id} not found!")
    return order


def _send_post(url: str) -> requests.Response:
    try:
        return _session.post(url)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)


def _send_get(url: str) -> requests.Response:
    try:
        return _session.get(url)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)


# ---------------------------------------------------------------------------
# Order CRUD routes
# ---------------------------------------------------------------------------

@app.post('/create/<user_id>')
def create_order(user_id: str):
    key = str(uuid.uuid4())
    value = msgpack.encode(OrderValue(paid=False, items=[], user_id=user_id, total_cost=0))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'order_id': key})


@app.post('/batch_init/<n>/<n_items>/<n_users>/<item_price>')
def batch_init_users(n: int, n_items: int, n_users: int, item_price: int):
    n = int(n)
    n_items = int(n_items)
    n_users = int(n_users)
    item_price = int(item_price)

    def generate_entry() -> OrderValue:
        user_id = random.randint(0, n_users - 1)
        item1_id = random.randint(0, n_items - 1)
        item2_id = random.randint(0, n_items - 1)
        return OrderValue(
            paid=False,
            items=[(f"{item1_id}", 1), (f"{item2_id}", 1)],
            user_id=f"{user_id}",
            total_cost=2 * item_price,
        )

    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(generate_entry()) for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@app.get('/find/<order_id>')
def find_order(order_id: str):
    order = _get_order_or_abort(order_id)
    return jsonify({
        "order_id": order_id,
        "paid": order.paid,
        "items": order.items,
        "user_id": order.user_id,
        "total_cost": order.total_cost,
    })


@app.post('/addItem/<order_id>/<item_id>/<quantity>')
def add_item(order_id: str, item_id: str, quantity: int):
    order = _get_order_or_abort(order_id)
    # Direct HTTP to stock service — no gateway hairpin
    item_reply = _send_get(f"{STOCK_SERVICE_URL}/find/{item_id}")
    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")
    item_json: dict = item_reply.json()
    order.items.append((item_id, int(quantity)))
    order.total_cost += int(quantity) * item_json["price"]
    try:
        db.set(order_id, msgpack.encode(order))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(
        f"Item: {item_id} added to: {order_id} price updated to: {order.total_cost}",
        status=200,
    )


# ---------------------------------------------------------------------------
# Checkout
#
# Step 1: Keep the existing sequential HTTP checkout logic, migrated from
# GATEWAY_URL to direct service URLs (STOCK_SERVICE_URL, PAYMENT_SERVICE_URL).
# The RabbitMQ-based coordinator replaces this in Step 2.
# ---------------------------------------------------------------------------

def _rollback_stock(removed_items: list[tuple[str, int]]) -> None:
    for item_id, quantity in removed_items:
        _send_post(f"{STOCK_SERVICE_URL}/add/{item_id}/{quantity}")


@app.post('/checkout/<order_id>')
def checkout(order_id: str):
    app.logger.debug("Checking out %s", order_id)
    order = _get_order_or_abort(order_id)

    if order.paid:
        return Response("Checkout successful", status=200)

    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in order.items:
        items_quantities[item_id] += quantity

    removed_items: list[tuple[str, int]] = []
    for item_id, quantity in items_quantities.items():
        stock_reply = _send_post(f"{STOCK_SERVICE_URL}/subtract/{item_id}/{quantity}")
        if stock_reply.status_code != 200:
            _rollback_stock(removed_items)
            abort(400, f'Out of stock on item_id: {item_id}')
        removed_items.append((item_id, quantity))

    user_reply = _send_post(
        f"{PAYMENT_SERVICE_URL}/pay/{order.user_id}/{order.total_cost}"
    )
    if user_reply.status_code != 200:
        _rollback_stock(removed_items)
        abort(400, "User out of credit")

    if not mark_order_paid(db, order_id):
        # Payment succeeded but we failed to mark paid — rollback
        _send_post(f"{PAYMENT_SERVICE_URL}/add_funds/{order.user_id}/{order.total_cost}")
        _rollback_stock(removed_items)
        abort(400, DB_ERROR_STR)

    app.logger.debug("Checkout successful for %s", order_id)
    return Response("Checkout successful", status=200)


# ---------------------------------------------------------------------------
# Internal routes (coordinator ↔ participants)
# ---------------------------------------------------------------------------

@app.get('/internal/tx_decision/<tx_id>')
def tx_decision(tx_id: str):
    """Return the durable coordinator decision for a transaction.

    Participants may query this during startup reconciliation to determine
    whether to commit or abort a prepared hold. They must not unilaterally
    release holds when the answer is 'unknown'.
    """
    decision = get_decision(db, tx_id)
    if decision is None:
        return jsonify({"decision": "unknown"})
    return jsonify({"decision": decision})


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
