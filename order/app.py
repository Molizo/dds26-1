import atexit
import logging
import os
import random
import threading
import time
import uuid

import pika
import redis
import requests

from flask import Flask, Response, abort, jsonify

try:
    from .messaging import build_rabbitmq_parameters, encode_internal_message, ensure_rabbitmq_topology
    from .models import (
        ORDER_STATUS_FAILED,
        ORDER_STATUS_PENDING,
        SAGA_STATE_COMPLETED,
        SAGA_STATE_FAILED,
        SAGA_STATE_PENDING,
        SAGA_TERMINAL_STATES,
        OrderValue,
        SagaValue,
        decode_order,
        decode_saga,
        encode_order,
        encode_saga,
        now_ms,
    )
except ImportError:
    from messaging import build_rabbitmq_parameters, encode_internal_message, ensure_rabbitmq_topology
    from models import (
        ORDER_STATUS_FAILED,
        ORDER_STATUS_PENDING,
        SAGA_STATE_COMPLETED,
        SAGA_STATE_FAILED,
        SAGA_STATE_PENDING,
        SAGA_TERMINAL_STATES,
        OrderValue,
        SagaValue,
        decode_order,
        decode_saga,
        encode_order,
        encode_saga,
        now_ms,
    )
from shared_messaging.contracts import CheckoutRequestedPayload, ItemQuantity, MessageMetadata

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ["GATEWAY_URL"]
STOCK_SERVICE_URL = os.environ.get("STOCK_SERVICE_URL", "")
STOCK_SERVICE_PORT = int(os.environ.get("STOCK_SERVICE_PORT", "5000"))

CHECKOUT_WAIT_TIMEOUT_MS = int(os.environ.get("CHECKOUT_WAIT_TIMEOUT_MS", "3000"))
CHECKOUT_WAIT_POLL_MS = int(os.environ.get("CHECKOUT_WAIT_POLL_MS", "50"))
CHECKOUT_WAIT_MAX_POLL_MS = int(os.environ.get("CHECKOUT_WAIT_MAX_POLL_MS", "200"))
CHECKOUT_LOCK_WAIT_MS = int(os.environ.get("CHECKOUT_LOCK_WAIT_MS", "1000"))
CHECKOUT_LOCK_EX_SEC = int(os.environ.get("CHECKOUT_LOCK_EX_SEC", "5"))
CHECKOUT_LOCK_POLL_MS = int(os.environ.get("CHECKOUT_LOCK_POLL_MS", "10"))

app = Flask("order-service")

db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
)

publisher_local = threading.local()


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


def close_publisher_connection() -> None:
    connection = getattr(publisher_local, "connection", None)
    if connection is not None:
        try:
            if connection.is_open:
                connection.close()
        except Exception:
            app.logger.debug("Publisher connection already closed during shutdown")
    publisher_local.connection = None
    publisher_local.channel = None


atexit.register(close_publisher_connection)


def saga_key(saga_id: str) -> str:
    return f"saga:{saga_id}"


def order_saga_key(order_id: str) -> str:
    return f"order_saga:{order_id}"


def checkout_lock_key(order_id: str) -> str:
    return f"checkout_lock:{order_id}"


def decode_str(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    return raw.decode("utf-8")


def get_order_from_db(order_id: str) -> OrderValue:
    try:
        entry_raw: bytes | None = db.get(order_id)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    order_entry = decode_order(entry_raw)
    if order_entry is None:
        abort(400, f"Order: {order_id} not found!")
    return order_entry


def get_saga_from_db(saga_id: str) -> SagaValue | None:
    try:
        entry_raw: bytes | None = db.get(saga_key(saga_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return decode_saga(entry_raw)


def put_order(order_id: str, order_entry: OrderValue) -> None:
    try:
        db.set(order_id, encode_order(order_entry))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def put_saga(saga_entry: SagaValue) -> None:
    saga_entry.updated_at_ms = now_ms()
    try:
        db.set(saga_key(saga_entry.saga_id), encode_saga(saga_entry))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def send_get_request(url: str):
    try:
        response = requests.get(url, timeout=3)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)
    return response


def normalize_service_base_url(raw: str, *, default_port: int) -> str:
    stripped = raw.strip()
    if not stripped:
        return ""
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return stripped.rstrip("/")
    if ":" in stripped:
        return f"http://{stripped.rstrip('/')}"
    return f"http://{stripped}:{default_port}"


def get_stock_lookup_url(item_id: str) -> str:
    stock_base = normalize_service_base_url(STOCK_SERVICE_URL, default_port=STOCK_SERVICE_PORT)
    if stock_base:
        return f"{stock_base}/find/{item_id}"
    return f"{GATEWAY_URL}/stock/find/{item_id}"


def reset_publisher_connection() -> None:
    connection = getattr(publisher_local, "connection", None)
    channel = getattr(publisher_local, "channel", None)
    if channel is not None:
        try:
            if channel.is_open:
                channel.close()
        except Exception:
            app.logger.debug("Publisher channel already closed")
    if connection is not None:
        try:
            if connection.is_open:
                connection.close()
        except Exception:
            app.logger.debug("Publisher connection already closed")
    publisher_local.connection = None
    publisher_local.channel = None


def get_checkout_publisher_channel():
    connection = getattr(publisher_local, "connection", None)
    channel = getattr(publisher_local, "channel", None)
    if connection is not None and channel is not None and connection.is_open and channel.is_open:
        return channel

    reset_publisher_connection()
    connection = pika.BlockingConnection(build_rabbitmq_parameters())
    channel = connection.channel()
    ensure_rabbitmq_topology(channel)
    publisher_local.connection = connection
    publisher_local.channel = channel
    return channel


def build_message_metadata(
    *,
    saga_id: str,
    order_id: str,
    step: str,
    correlation_id: str,
    causation_id: str,
    attempt: int = 0,
) -> MessageMetadata:
    return MessageMetadata(
        message_id=str(uuid.uuid4()),
        saga_id=saga_id,
        order_id=order_id,
        step=step,
        attempt=attempt,
        timestamp=now_ms(),
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


def publish_checkout_requested(saga_entry: SagaValue) -> None:
    metadata = build_message_metadata(
        saga_id=saga_entry.saga_id,
        order_id=saga_entry.order_id,
        step="checkout_requested",
        correlation_id=saga_entry.saga_id,
        causation_id=saga_entry.saga_id,
    )
    payload = CheckoutRequestedPayload(
        user_id=saga_entry.user_id,
        total_cost=saga_entry.total_cost,
        items=saga_entry.items,
    )
    body = encode_internal_message("CheckoutRequested", metadata, payload)

    for attempt in range(2):
        channel = get_checkout_publisher_channel()
        try:
            channel.basic_publish(
                exchange="checkout.command",
                routing_key="checkout.command",
                body=body,
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                    message_id=metadata.message_id,
                    correlation_id=metadata.correlation_id,
                    timestamp=int(time.time()),
                    type="CheckoutRequested",
                ),
                mandatory=False,
            )
            return
        except Exception:
            reset_publisher_connection()
            if attempt == 1:
                raise


def aggregate_items(items: list[tuple[str, int]]) -> list[ItemQuantity]:
    item_totals: dict[str, int] = {}
    for item_id, quantity in items:
        item_totals[item_id] = item_totals.get(item_id, 0) + int(quantity)
    return [ItemQuantity(item_id=item_id, quantity=quantity) for item_id, quantity in item_totals.items()]


def update_order_checkout_state(order_id: str, status: str, reason: str | None = None) -> None:
    order_entry = get_order_from_db(order_id)
    order_entry.checkout_status = status
    order_entry.checkout_failure_reason = reason
    put_order(order_id, order_entry)


def mark_saga_failed(saga_entry: SagaValue, reason: str) -> None:
    saga_entry.state = SAGA_STATE_FAILED
    saga_entry.failure_reason = reason
    put_saga(saga_entry)
    update_order_checkout_state(saga_entry.order_id, ORDER_STATUS_FAILED, reason)


def get_order_active_saga_id(order_id: str) -> str | None:
    try:
        return decode_str(db.get(order_saga_key(order_id)))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def set_order_saga_id(order_id: str, saga_id: str) -> None:
    try:
        db.set(order_saga_key(order_id), saga_id)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def acquire_checkout_lock(order_id: str) -> str | None:
    deadline = now_ms() + CHECKOUT_LOCK_WAIT_MS
    lock_key = checkout_lock_key(order_id)
    token = str(uuid.uuid4())
    poll_ms = max(1, CHECKOUT_LOCK_POLL_MS)
    while now_ms() < deadline:
        try:
            acquired = db.set(lock_key, token, nx=True, ex=CHECKOUT_LOCK_EX_SEC)
        except redis.exceptions.RedisError:
            abort(400, DB_ERROR_STR)
        if acquired:
            return token
        time.sleep(poll_ms / 1000)
    return None


def release_checkout_lock(order_id: str, token: str | None) -> None:
    if token is None:
        return
    try:
        db.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
            1,
            checkout_lock_key(order_id),
            token,
        )
    except redis.exceptions.RedisError:
        app.logger.warning("Failed to release checkout lock for order=%s", order_id)


def ensure_saga_for_checkout(order_id: str, order_entry: OrderValue) -> tuple[SagaValue, bool]:
    existing_saga_id = get_order_active_saga_id(order_id)
    if existing_saga_id is not None:
        existing_saga = get_saga_from_db(existing_saga_id)
        if existing_saga is not None and existing_saga.state not in SAGA_TERMINAL_STATES:
            return existing_saga, False

    new_saga_id = str(uuid.uuid4())
    saga_entry = SagaValue(
        saga_id=new_saga_id,
        order_id=order_id,
        user_id=order_entry.user_id,
        items=aggregate_items(order_entry.items),
        total_cost=order_entry.total_cost,
        state=SAGA_STATE_PENDING,
        updated_at_ms=now_ms(),
    )
    put_saga(saga_entry)
    set_order_saga_id(order_id, new_saga_id)
    order_entry.checkout_status = ORDER_STATUS_PENDING
    order_entry.checkout_failure_reason = None
    put_order(order_id, order_entry)
    return saga_entry, True


def wait_for_terminal_saga(saga_id: str) -> SagaValue | None:
    deadline = now_ms() + CHECKOUT_WAIT_TIMEOUT_MS
    poll_ms = max(5, CHECKOUT_WAIT_POLL_MS)
    max_poll_ms = max(poll_ms, CHECKOUT_WAIT_MAX_POLL_MS)
    while now_ms() < deadline:
        saga_entry = get_saga_from_db(saga_id)
        if saga_entry is None:
            return None
        if saga_entry.state in SAGA_TERMINAL_STATES:
            return saga_entry
        time.sleep(poll_ms / 1000)
        poll_ms = min(poll_ms * 2, max_poll_ms)
    return None


def finalize_checkout(saga_id: str) -> Response:
    terminal = wait_for_terminal_saga(saga_id)
    if terminal is None:
        latest = get_saga_from_db(saga_id)
        if latest is not None and latest.state not in SAGA_TERMINAL_STATES:
            mark_saga_failed(latest, "timeout")
        abort(400, "Checkout timed out")

    if terminal.state == SAGA_STATE_COMPLETED:
        return Response("Checkout successful", status=200)

    reason = terminal.failure_reason or "checkout_failed"
    abort(400, f"Checkout failed: {reason}")


@app.post("/create/<user_id>")
def create_order(user_id: str):
    key = str(uuid.uuid4())
    value = encode_order(OrderValue(paid=False, items=[], user_id=user_id, total_cost=0))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"order_id": key})


@app.post("/batch_init/<n>/<n_items>/<n_users>/<item_price>")
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

    kv_pairs: dict[str, bytes] = {f"{i}": encode_order(generate_entry()) for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@app.get("/find/<order_id>")
def find_order(order_id: str):
    order_entry = get_order_from_db(order_id)
    return jsonify(
        {
            "order_id": order_id,
            "paid": order_entry.paid,
            "items": order_entry.items,
            "user_id": order_entry.user_id,
            "total_cost": order_entry.total_cost,
        }
    )


@app.post("/addItem/<order_id>/<item_id>/<quantity>")
def add_item(order_id: str, item_id: str, quantity: int):
    order_entry = get_order_from_db(order_id)
    item_reply = send_get_request(get_stock_lookup_url(item_id))
    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")

    item_json: dict = item_reply.json()
    order_entry.items.append((item_id, int(quantity)))
    order_entry.total_cost += int(quantity) * item_json["price"]
    put_order(order_id, order_entry)
    return Response(
        f"Item: {item_id} added to: {order_id} price updated to: {order_entry.total_cost}",
        status=200,
    )


@app.post("/checkout/<order_id>")
def checkout(order_id: str):
    app.logger.debug("Checking out %s", order_id)
    order_entry = get_order_from_db(order_id)
    if order_entry.paid:
        abort(400, f"Order: {order_id} has already been paid")

    lock_token = acquire_checkout_lock(order_id)
    if lock_token is None:
        existing_saga_id = get_order_active_saga_id(order_id)
        if existing_saga_id is None:
            abort(400, "Checkout already in progress")
        return finalize_checkout(existing_saga_id)

    try:
        saga_entry, created_now = ensure_saga_for_checkout(order_id, order_entry)
        if created_now:
            try:
                publish_checkout_requested(saga_entry)
            except Exception as exc:
                mark_saga_failed(saga_entry, "internal_failure")
                app.logger.exception(
                    "Failed to publish CheckoutRequested for order=%s saga=%s",
                    order_id,
                    saga_entry.saga_id,
                )
                abort(400, f"Checkout failed: {exc}")
    finally:
        release_checkout_lock(order_id, lock_token)

    return finalize_checkout(saga_entry.saga_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
