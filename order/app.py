import atexit
import logging
import os
import random
import time
import uuid

import redis
import requests
from flask import Flask, Response, abort, jsonify, request
from msgspec import msgpack

from store import (
    OrderValue,
    get_order,
    mark_order_paid,
    read_order_snapshot,
)

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"
REQUEST_TIMEOUT_SECONDS = 5.0
CHECKOUT_PROXY_TIMEOUT_SECONDS = 45.0
REQUEST_RETRY_COUNT = 3
ORDER_UPDATE_RETRY_COUNT = 10
MUTATION_GUARD_RETRY_COUNT = 20
MUTATION_GUARD_RETRY_DELAY_SECONDS = 0.05

STOCK_SERVICE_URL = os.environ["STOCK_SERVICE_URL"]
ORCHESTRATOR_SERVICE_URL = os.environ["ORCHESTRATOR_SERVICE_URL"]

app = Flask("order-service")

db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
)


def close_db_connection():
    db.close()


atexit.register(close_db_connection)

_session = requests.Session()


def _get_order_or_abort(order_id: str) -> OrderValue:
    order = get_order(db, order_id)
    if order is None:
        abort(400, f"Order: {order_id} not found!")
    return order


def _require_positive_int(value: int | str, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        abort(400, f"{field_name} must be a positive integer")
    if parsed <= 0:
        abort(400, f"{field_name} must be a positive integer")
    return parsed


def _send_get(url: str) -> requests.Response:
    for attempt in range(REQUEST_RETRY_COUNT):
        try:
            return _session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as exc:
            app.logger.warning(
                "GET %s attempt %s/%s failed: %s",
                url,
                attempt + 1,
                REQUEST_RETRY_COUNT,
                exc,
            )
    abort(400, REQ_ERROR_STR)


def _send_post(
    url: str,
    *,
    json_body: dict | None = None,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> requests.Response:
    for attempt in range(REQUEST_RETRY_COUNT):
        try:
            return _session.post(url, json=json_body, timeout=timeout_seconds)
        except requests.exceptions.RequestException as exc:
            app.logger.warning(
                "POST %s attempt %s/%s failed: %s",
                url,
                attempt + 1,
                REQUEST_RETRY_COUNT,
                exc,
            )
    abort(400, REQ_ERROR_STR)


def _acquire_mutation_guard(order_id: str) -> str:
    lease_id = str(uuid.uuid4())

    for _ in range(MUTATION_GUARD_RETRY_COUNT):
        response = _send_post(
            f"{ORCHESTRATOR_SERVICE_URL}/internal/orders/{order_id}/mutation_guard/acquire",
            json_body={"lease_id": lease_id},
        )
        if response.status_code == 200:
            return lease_id
        if response.status_code == 409:
            reason = response.json().get("reason")
            if reason == "mutation_in_progress":
                time.sleep(MUTATION_GUARD_RETRY_DELAY_SECONDS)
                continue
            abort(409, "Checkout already in progress")
        abort(400, "Could not verify checkout state")

    abort(409, "Another order mutation is already in progress")


def _release_mutation_guard(order_id: str, lease_id: str) -> None:
    try:
        _send_post(
            f"{ORCHESTRATOR_SERVICE_URL}/internal/orders/{order_id}/mutation_guard/release",
            json_body={"lease_id": lease_id},
        )
    except Exception:
        app.logger.exception(
            "Failed releasing order mutation guard order=%s lease=%s",
            order_id,
            lease_id,
        )


@app.post("/create/<user_id>")
def create_order(user_id: str):
    key = str(uuid.uuid4())
    value = msgpack.encode(OrderValue(paid=False, items=[], user_id=user_id, total_cost=0))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"order_id": key})


@app.post("/batch_init/<n>/<n_items>/<n_users>/<item_price>")
def batch_init_users(n: int, n_items: int, n_users: int, item_price: int):
    n = int(n)
    n_items = int(n_items)
    n_users = int(n_users)
    item_price = int(item_price)

    def generate_entry() -> OrderValue:
        user_id = random.randint(0, max(0, n_users - 1))
        item1_id = random.randint(0, max(0, n_items - 1))
        item2_id = random.randint(0, max(0, n_items - 1))
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


@app.get("/find/<order_id>")
def find_order(order_id: str):
    order = _get_order_or_abort(order_id)
    return jsonify(
        {
            "order_id": order_id,
            "paid": order.paid,
            "items": order.items,
            "user_id": order.user_id,
            "total_cost": order.total_cost,
        }
    )


@app.post("/addItem/<order_id>/<item_id>/<quantity>")
def add_item(order_id: str, item_id: str, quantity: int):
    quantity = _require_positive_int(quantity, "quantity")
    item_reply = _send_get(f"{STOCK_SERVICE_URL}/find/{item_id}")
    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")

    item_json: dict = item_reply.json()
    price = int(item_json["price"])
    order_paid_key = f"order_paid:{order_id}"
    lease_id = _acquire_mutation_guard(order_id)

    try:
        for _ in range(ORDER_UPDATE_RETRY_COUNT):
            try:
                with db.pipeline() as pipe:
                    pipe.watch(order_id, order_paid_key)
                    raw_order = pipe.get(order_id)
                    if raw_order is None:
                        pipe.unwatch()
                        abort(400, f"Order: {order_id} not found!")

                    order = msgpack.decode(raw_order, type=OrderValue)
                    if order.paid or pipe.exists(order_paid_key) == 1:
                        pipe.unwatch()
                        abort(409, "Order already paid")

                    order.items.append((item_id, quantity))
                    order.total_cost += quantity * price

                    pipe.multi()
                    pipe.set(order_id, msgpack.encode(order))
                    pipe.execute()
                    return Response(
                        f"Item: {item_id} added to: {order_id} price updated to: {order.total_cost}",
                        status=200,
                    )
            except redis.WatchError:
                continue
            except redis.exceptions.RedisError:
                return abort(400, DB_ERROR_STR)
    finally:
        _release_mutation_guard(order_id, lease_id)

    abort(409, "Conflict: could not update order")


@app.post("/checkout/<order_id>")
def checkout(order_id: str):
    response = _send_post(
        f"{ORCHESTRATOR_SERVICE_URL}/checkout/{order_id}",
        timeout_seconds=CHECKOUT_PROXY_TIMEOUT_SECONDS,
    )

    passthrough = Response(response.content, status=response.status_code)
    content_type = response.headers.get("Content-Type")
    if content_type:
        passthrough.headers["Content-Type"] = content_type
    return passthrough


@app.get("/internal/orders/<order_id>")
def internal_get_order(order_id: str):
    snapshot = read_order_snapshot(db, order_id)
    if snapshot is None:
        abort(404, f"Order: {order_id} not found!")
    return jsonify(
        {
            "order_id": snapshot.order_id,
            "user_id": snapshot.user_id,
            "total_cost": snapshot.total_cost,
            "paid": snapshot.paid,
            "items": snapshot.items,
        }
    )


@app.post("/internal/orders/<order_id>/mark_paid")
def internal_mark_paid(order_id: str):
    payload = request.get_json(silent=True) or {}
    tx_id = payload.get("tx_id")
    if not tx_id:
        abort(400, "tx_id is required")

    if not mark_order_paid(db, order_id):
        abort(404, f"Order: {order_id} not found!")
    return jsonify({"done": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
