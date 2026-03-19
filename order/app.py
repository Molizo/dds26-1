import atexit
import logging
import os
import random
import time
import uuid

import redis
import requests
from flask import Flask, Response, abort, jsonify
from msgspec import msgpack

from common.constants import (
    ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD,
    ORCHESTRATOR_CMD_CHECKOUT,
    ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD,
    ORCHESTRATOR_COMMANDS_QUEUE,
)
from common.models import (
    InternalCommand,
    InternalReply,
    decode_internal_reply,
    encode_internal_command,
)
from common.rpc import rpc_request_bytes
from store import OrderValue, get_order

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"
REQUEST_TIMEOUT_SECONDS = 5.0
RPC_TIMEOUT_SECONDS = 5.0
CHECKOUT_RPC_TIMEOUT_SECONDS = 45.0
REQUEST_RETRY_COUNT = 3
ORDER_UPDATE_RETRY_COUNT = 10
MUTATION_GUARD_RETRY_COUNT = 20
MUTATION_GUARD_RETRY_DELAY_SECONDS = 0.05

STOCK_SERVICE_URL = os.environ["STOCK_SERVICE_URL"]
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

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


def _call_orchestrator(
    command: str,
    order_id: str,
    *,
    tx_id: str | None = None,
    lease_id: str | None = None,
    timeout_seconds: float = RPC_TIMEOUT_SECONDS,
) -> InternalReply | None:
    request_id = str(uuid.uuid4())
    payload = encode_internal_command(
        InternalCommand(
            request_id=request_id,
            command=command,
            order_id=order_id,
            tx_id=tx_id,
            lease_id=lease_id,
        )
    )
    reply = rpc_request_bytes(
        RABBITMQ_URL,
        ORCHESTRATOR_COMMANDS_QUEUE,
        request_id=request_id,
        body=payload,
        timeout_seconds=timeout_seconds,
    )
    if reply is None:
        return None
    return decode_internal_reply(reply)


def _release_mutation_guard(order_id: str, lease_id: str) -> None:
    try:
        _call_orchestrator(
            ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD,
            order_id,
            lease_id=lease_id,
            timeout_seconds=RPC_TIMEOUT_SECONDS,
        )
    except Exception:
        app.logger.exception(
            "Failed releasing order mutation guard order=%s lease=%s",
            order_id,
            lease_id,
        )


def _acquire_mutation_guard(order_id: str) -> str:
    lease_id = str(uuid.uuid4())

    for _ in range(MUTATION_GUARD_RETRY_COUNT):
        try:
            reply = _call_orchestrator(
                ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD,
                order_id,
                lease_id=lease_id,
                timeout_seconds=RPC_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            app.logger.warning("Acquire mutation guard failed order=%s lease=%s: %s", order_id, lease_id, exc)
            time.sleep(MUTATION_GUARD_RETRY_DELAY_SECONDS)
            continue

        if reply is None:
            time.sleep(MUTATION_GUARD_RETRY_DELAY_SECONDS)
            continue
        if reply.ok and reply.status_code == 200:
            return lease_id
        if reply.status_code == 409 and reply.reason == "mutation_in_progress":
            time.sleep(MUTATION_GUARD_RETRY_DELAY_SECONDS)
            continue
        if reply.status_code == 409 and reply.reason == "checkout_in_progress":
            abort(409, "Checkout already in progress")
        abort(reply.status_code, reply.error or "Could not verify checkout state")

    _release_mutation_guard(order_id, lease_id)
    abort(400, "Could not verify checkout state")


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


def _precheck_order_for_add_item(order_id: str) -> None:
    order_paid_key = f"order_paid:{order_id}"
    try:
        order = get_order(db, order_id)
        if order is None:
            abort(400, f"Order: {order_id} not found!")
        if order.paid or db.exists(order_paid_key) == 1:
            abort(409, "Order already paid")
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


@app.post("/addItem/<order_id>/<item_id>/<quantity>")
def add_item(order_id: str, item_id: str, quantity: int):
    quantity = _require_positive_int(quantity, "quantity")
    item_reply = _send_get(f"{STOCK_SERVICE_URL}/find/{item_id}")
    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")

    _precheck_order_for_add_item(order_id)
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
    tx_id = str(uuid.uuid4())
    try:
        reply = _call_orchestrator(
            ORCHESTRATOR_CMD_CHECKOUT,
            order_id,
            tx_id=tx_id,
            timeout_seconds=CHECKOUT_RPC_TIMEOUT_SECONDS,
        )
    except Exception:
        app.logger.exception("Checkout RPC failed order=%s tx=%s", order_id, tx_id)
        abort(400, "Checkout unavailable")

    if reply is None:
        abort(400, "Checkout result unavailable")
    if reply.ok:
        return Response("Checkout successful", status=200)
    abort(reply.status_code, reply.error or "Checkout failed")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
