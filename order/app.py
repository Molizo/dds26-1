import logging
import os
import atexit
import random
import time
import uuid
from collections import defaultdict

import redis
import requests

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response


DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']
CHECKOUT_PROTOCOL = os.environ.get("CHECKOUT_PROTOCOL", "saga").strip().lower()
VALID_CHECKOUT_PROTOCOLS = {"saga", "2pc"}
ORDER_LOCK_TTL_SECONDS = int(os.environ.get("ORDER_LOCK_TTL_SECONDS", "30"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "3"))
RECENT_CHECKOUT_TTL_SECONDS = int(os.environ.get("RECENT_CHECKOUT_TTL_SECONDS", "2"))
ORDER_LOCK_KEY_PREFIX = "checkout_lock:"
TX_KEY_PREFIX = "checkout_tx:"
ORDER_ACTIVE_TX_PREFIX = "checkout_order_active_tx:"
ORDER_RECENT_CHECKOUT_PREFIX = "checkout_order_recent:"
STATUS_INIT = "INIT"
STATUS_STOCK_RESERVED = "STOCK_RESERVED"
STATUS_PAYMENT_CHARGED = "PAYMENT_CHARGED"
STATUS_COMPENSATING = "COMPENSATING"
STATUS_ABORTED = "ABORTED"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED_NEEDS_RECOVERY = "FAILED_NEEDS_RECOVERY"
TERMINAL_TX_STATUSES = {STATUS_ABORTED, STATUS_COMPLETED, STATUS_FAILED_NEEDS_RECOVERY}
LOCK_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

if CHECKOUT_PROTOCOL not in VALID_CHECKOUT_PROTOCOLS:
    raise RuntimeError(
        f"Invalid CHECKOUT_PROTOCOL '{CHECKOUT_PROTOCOL}'. "
        "Expected one of: saga, 2pc"
    )

app = Flask("order-service")

db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
                              port=int(os.environ['REDIS_PORT']),
                              password=os.environ['REDIS_PASSWORD'],
                              db=int(os.environ['REDIS_DB']))


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


class OrderValue(Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int


class CheckoutTxValue(Struct):
    tx_id: str
    order_id: str
    user_id: str
    total_cost: int
    protocol: str
    status: str
    stock_removed: list[tuple[str, int]]
    payment_applied: bool
    started_at_ms: int
    updated_at_ms: int
    last_error: str | None


def now_ms() -> int:
    return int(time.time() * 1000)


def tx_key(tx_id: str) -> str:
    return f"{TX_KEY_PREFIX}{tx_id}"


def active_tx_key(order_id: str) -> str:
    return f"{ORDER_ACTIVE_TX_PREFIX}{order_id}"


def lock_key(order_id: str) -> str:
    return f"{ORDER_LOCK_KEY_PREFIX}{order_id}"


def recent_checkout_key(order_id: str) -> str:
    return f"{ORDER_RECENT_CHECKOUT_PREFIX}{order_id}"


def is_terminal_status(status: str) -> bool:
    return status in TERMINAL_TX_STATUSES


def save_tx(tx_entry: CheckoutTxValue):
    tx_entry.updated_at_ms = now_ms()
    try:
        db.set(tx_key(tx_entry.tx_id), msgpack.encode(tx_entry))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def get_tx(tx_id: str) -> CheckoutTxValue | None:
    try:
        entry: bytes = db.get(tx_key(tx_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return msgpack.decode(entry, type=CheckoutTxValue) if entry else None


def set_active_tx(order_id: str, tx_id: str):
    try:
        db.set(active_tx_key(order_id), tx_id)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def get_active_tx(order_id: str) -> CheckoutTxValue | None:
    try:
        tx_id = db.get(active_tx_key(order_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if tx_id is None:
        return None
    return get_tx(tx_id.decode())


def clear_active_tx(order_id: str):
    try:
        db.delete(active_tx_key(order_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def mark_recent_checkout(order_id: str):
    try:
        db.set(recent_checkout_key(order_id), "1", ex=RECENT_CHECKOUT_TTL_SECONDS)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def has_recent_checkout(order_id: str) -> bool:
    try:
        return bool(db.exists(recent_checkout_key(order_id)))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def acquire_checkout_lock(order_id: str) -> str | None:
    token = str(uuid.uuid4())
    try:
        acquired = db.set(lock_key(order_id), token, nx=True, ex=ORDER_LOCK_TTL_SECONDS)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if not acquired:
        return None
    return token


def release_checkout_lock(order_id: str, token: str):
    try:
        db.eval(LOCK_RELEASE_SCRIPT, 1, lock_key(order_id), token)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def get_order_from_db(order_id: str) -> OrderValue | None:
    try:
        # get serialized data
        entry: bytes = db.get(order_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    # deserialize data if it exists else return null
    entry: OrderValue | None = msgpack.decode(entry, type=OrderValue) if entry else None
    if entry is None:
        # if order does not exist in the database; abort
        abort(400, f"Order: {order_id} not found!")
    return entry


@app.post('/create/<user_id>')
def create_order(user_id: str):
    user_reply = send_get_request(f"{GATEWAY_URL}/payment/find_user/{user_id}")
    if user_reply.status_code != 200:
        abort(400, f"User: {user_id} not found!")

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
        value = OrderValue(paid=False,
                           items=[(f"{item1_id}", 1), (f"{item2_id}", 1)],
                           user_id=f"{user_id}",
                           total_cost=2*item_price)
        return value

    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(generate_entry())
                                  for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@app.get('/find/<order_id>')
def find_order(order_id: str):
    order_entry: OrderValue = get_order_from_db(order_id)
    return jsonify(
        {
            "order_id": order_id,
            "paid": order_entry.paid,
            "items": order_entry.items,
            "user_id": order_entry.user_id,
            "total_cost": order_entry.total_cost
        }
    )


def send_post_request(url: str):
    try:
        response = requests.post(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException:
        return None
    else:
        return response


def send_get_request(url: str):
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)
    else:
        return response


@app.post('/addItem/<order_id>/<item_id>/<quantity>')
def add_item(order_id: str, item_id: str, quantity: int):
    if int(quantity) <= 0:
        abort(400, f"Quantity must be positive, got {quantity}")

    order_entry: OrderValue = get_order_from_db(order_id)
    item_reply = send_get_request(f"{GATEWAY_URL}/stock/find/{item_id}")
    if item_reply.status_code != 200:
        # Request failed because item does not exist
        abort(400, f"Item: {item_id} does not exist!")
    item_json: dict = item_reply.json()
    order_entry.items.append((item_id, int(quantity)))
    order_entry.total_cost += int(quantity) * item_json["price"]
    try:
        db.set(order_id, msgpack.encode(order_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} added to: {order_id} price updated to: {order_entry.total_cost}",
                    status=200)


def rollback_stock(removed_items: list[tuple[str, int]]) -> bool:
    for item_id, quantity in removed_items:
        add_reply = send_post_request(f"{GATEWAY_URL}/stock/add/{item_id}/{quantity}")
        if add_reply is None or add_reply.status_code != 200:
            return False
    return True


def aggregate_items(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
    item_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in items:
        item_quantities[item_id] += quantity
    return sorted(item_quantities.items(), key=lambda item: item[0])


def create_checkout_tx(order_entry: OrderValue, order_id: str) -> CheckoutTxValue:
    now = now_ms()
    return CheckoutTxValue(
        tx_id=str(uuid.uuid4()),
        order_id=order_id,
        user_id=order_entry.user_id,
        total_cost=order_entry.total_cost,
        protocol=CHECKOUT_PROTOCOL,
        status=STATUS_INIT,
        stock_removed=[],
        payment_applied=False,
        started_at_ms=now,
        updated_at_ms=now,
        last_error=None,
    )


def mark_tx_failed(tx_entry: CheckoutTxValue, message: str):
    tx_entry.status = STATUS_FAILED_NEEDS_RECOVERY
    tx_entry.last_error = message
    save_tx(tx_entry)


def run_saga_checkout(order_id: str, order_entry: OrderValue, tx_entry: CheckoutTxValue) -> Response:
    items_quantities = aggregate_items(order_entry.items)
    for item_id, quantity in items_quantities:
        stock_reply = send_post_request(f"{GATEWAY_URL}/stock/subtract/{item_id}/{quantity}")
        if stock_reply is None:
            tx_entry.status = STATUS_COMPENSATING
            tx_entry.last_error = "Stock request failed during subtract"
            save_tx(tx_entry)
            if rollback_stock(tx_entry.stock_removed):
                tx_entry.status = STATUS_ABORTED
                tx_entry.last_error = REQ_ERROR_STR
                save_tx(tx_entry)
            else:
                mark_tx_failed(tx_entry, "Could not compensate stock after subtract request failure")
            abort(400, REQ_ERROR_STR)
        if stock_reply.status_code != 200:
            tx_entry.status = STATUS_COMPENSATING
            tx_entry.last_error = f"Out of stock on item_id: {item_id}"
            save_tx(tx_entry)
            if rollback_stock(tx_entry.stock_removed):
                tx_entry.status = STATUS_ABORTED
                save_tx(tx_entry)
            else:
                mark_tx_failed(tx_entry, f"Could not compensate stock for item {item_id}")
            abort(400, f"Out of stock on item_id: {item_id}")

        tx_entry.stock_removed.append((item_id, quantity))
        tx_entry.status = STATUS_STOCK_RESERVED
        save_tx(tx_entry)

    user_reply = send_post_request(f"{GATEWAY_URL}/payment/pay/{order_entry.user_id}/{order_entry.total_cost}")
    if user_reply is None:
        tx_entry.status = STATUS_COMPENSATING
        tx_entry.last_error = "Payment request failed during pay"
        save_tx(tx_entry)
        if rollback_stock(tx_entry.stock_removed):
            tx_entry.status = STATUS_ABORTED
            tx_entry.last_error = REQ_ERROR_STR
            save_tx(tx_entry)
        else:
            mark_tx_failed(tx_entry, "Could not compensate stock after payment request failure")
        abort(400, REQ_ERROR_STR)

    if user_reply.status_code != 200:
        tx_entry.status = STATUS_COMPENSATING
        tx_entry.last_error = "User out of credit"
        save_tx(tx_entry)
        if rollback_stock(tx_entry.stock_removed):
            tx_entry.status = STATUS_ABORTED
            save_tx(tx_entry)
        else:
            mark_tx_failed(tx_entry, "Could not compensate stock after payment rejection")
        abort(400, "User out of credit")

    tx_entry.payment_applied = True
    tx_entry.status = STATUS_PAYMENT_CHARGED
    tx_entry.last_error = None
    save_tx(tx_entry)

    order_entry.paid = True
    try:
        db.set(order_id, msgpack.encode(order_entry))
    except redis.exceptions.RedisError:
        mark_tx_failed(tx_entry, "Order DB write failed while marking order paid")
        abort(400, DB_ERROR_STR)

    tx_entry.status = STATUS_COMPLETED
    tx_entry.last_error = None
    save_tx(tx_entry)
    app.logger.debug("Checkout successful")
    return Response("Checkout successful", status=200)


def run_checkout_protocol(order_id: str, order_entry: OrderValue, tx_entry: CheckoutTxValue) -> Response:
    if CHECKOUT_PROTOCOL == "saga":
        return run_saga_checkout(order_id, order_entry, tx_entry)
    abort(400, "CHECKOUT_PROTOCOL=2pc is configured but not implemented in steps 1-3")


@app.post('/checkout/<order_id>')
def checkout(order_id: str):
    app.logger.debug(f"Checking out {order_id}")
    lock_token = acquire_checkout_lock(order_id)
    if lock_token is None:
        abort(409, f"Checkout already in progress for order: {order_id}")

    tx_entry: CheckoutTxValue | None = None
    try:
        order_entry: OrderValue = get_order_from_db(order_id)
        if order_entry.paid:
            if has_recent_checkout(order_id):
                abort(409, f"Checkout already completed for order: {order_id}")
            return Response("Order already paid", status=200)

        active_tx = get_active_tx(order_id)
        if active_tx and not is_terminal_status(active_tx.status):
            abort(409, f"Checkout already in progress for order: {order_id}")

        mark_recent_checkout(order_id)
        tx_entry = create_checkout_tx(order_entry, order_id)
        save_tx(tx_entry)
        set_active_tx(order_id, tx_entry.tx_id)

        response = run_checkout_protocol(order_id, order_entry, tx_entry)
        return response
    finally:
        if tx_entry and is_terminal_status(tx_entry.status):
            clear_active_tx(order_id)
        release_checkout_lock(order_id, lock_token)


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
