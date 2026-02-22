import atexit
import logging
import os
import random
import time
import uuid
from collections import defaultdict

import redis
import requests
from flask import Flask, Response, abort, jsonify
from msgspec import Struct, msgpack

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ["GATEWAY_URL"]
PAYMENT_INTERNAL_URL = os.environ["PAYMENT_INTERNAL_URL"].rstrip("/")
STOCK_INTERNAL_URL = os.environ["STOCK_INTERNAL_URL"].rstrip("/")
CHECKOUT_PROTOCOL = os.environ.get("CHECKOUT_PROTOCOL", "saga").strip().lower()
VALID_CHECKOUT_PROTOCOLS = {"saga", "2pc"}

ORDER_LOCK_TTL_SECONDS = int(os.environ.get("ORDER_LOCK_TTL_SECONDS", "30"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "3"))
RECENT_CHECKOUT_TTL_SECONDS = int(os.environ.get("RECENT_CHECKOUT_TTL_SECONDS", "2"))

ORDER_LOCK_KEY_PREFIX = "checkout_lock:"
TX_KEY_PREFIX = "checkout_tx:"
ORDER_ACTIVE_TX_PREFIX = "checkout_order_active_tx:"
ORDER_RECENT_CHECKOUT_PREFIX = "checkout_order_recent:"
ORDER_COMMIT_FENCE_PREFIX = "checkout_order_commit_fence:"

STATUS_INIT = "INIT"
STATUS_SAGA_STOCK_RESERVED = "SAGA_STOCK_RESERVED"
STATUS_SAGA_PAYMENT_CHARGED = "SAGA_PAYMENT_CHARGED"
STATUS_SAGA_COMPENSATING = "SAGA_COMPENSATING"
STATUS_2PC_PREPARING = "2PC_PREPARING"
STATUS_2PC_PREPARED = "2PC_PREPARED"
STATUS_2PC_ABORTING = "2PC_ABORTING"
STATUS_2PC_COMMITTING = "2PC_COMMITTING"
STATUS_2PC_FINALIZATION_PENDING = "2PC_FINALIZATION_PENDING"
STATUS_ABORTED = "ABORTED"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED_NEEDS_RECOVERY = "FAILED_NEEDS_RECOVERY"
TERMINAL_TX_STATUSES = {
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED_NEEDS_RECOVERY,
    STATUS_2PC_FINALIZATION_PENDING,
}

DECISION_COMMIT = "COMMIT"
DECISION_ABORT = "ABORT"

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

db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
)


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
    items_snapshot: list[tuple[str, int]]
    stock_prepared: list[tuple[str, int]]
    stock_committed: list[tuple[str, int]]
    stock_compensated: list[tuple[str, int]]
    payment_prepared: bool
    payment_committed: bool
    payment_reversed: bool
    decision: str | None
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


def commit_fence_key(order_id: str) -> str:
    return f"{ORDER_COMMIT_FENCE_PREFIX}{order_id}"


def is_terminal_status(status: str) -> bool:
    return status in TERMINAL_TX_STATUSES


def save_tx(tx_entry: CheckoutTxValue):
    tx_entry.updated_at_ms = now_ms()
    try:
        db.set(tx_key(tx_entry.tx_id), msgpack.encode(tx_entry))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def try_save_tx(tx_entry: CheckoutTxValue) -> bool:
    tx_entry.updated_at_ms = now_ms()
    try:
        db.set(tx_key(tx_entry.tx_id), msgpack.encode(tx_entry))
        return True
    except redis.exceptions.RedisError:
        return False


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
        active_id = db.get(active_tx_key(order_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    if active_id is None:
        return None
    return get_tx(active_id.decode())


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


def set_commit_fence(order_id: str, tx_id: str):
    try:
        db.set(commit_fence_key(order_id), tx_id)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def clear_commit_fence(order_id: str):
    try:
        db.delete(commit_fence_key(order_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def has_commit_fence(order_id: str) -> bool:
    try:
        return bool(db.exists(commit_fence_key(order_id)))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def get_commit_fence_tx_id(order_id: str) -> str | None:
    try:
        value = db.get(commit_fence_key(order_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    return value.decode() if value else None


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


def get_order_from_db(order_id: str) -> OrderValue:
    try:
        entry: bytes = db.get(order_id)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)
    decoded: OrderValue | None = msgpack.decode(entry, type=OrderValue) if entry else None
    if decoded is None:
        abort(400, f"Order: {order_id} not found!")
    return decoded


def persist_order(order_id: str, order_entry: OrderValue) -> bool:
    try:
        db.set(order_id, msgpack.encode(order_entry))
    except redis.exceptions.RedisError:
        return False
    return True


def send_post_request(url: str):
    try:
        response = requests.post(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException:
        return None
    return response


def send_get_request(url: str):
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)
    return response


def stock_internal_post(path: str):
    return send_post_request(f"{STOCK_INTERNAL_URL}{path}")


def payment_internal_post(path: str):
    return send_post_request(f"{PAYMENT_INTERNAL_URL}{path}")


def is_ok_response(reply: requests.Response | None) -> bool:
    return reply is not None and reply.status_code == 200


def contains_pair(items: list[tuple[str, int]], pair: tuple[str, int]) -> bool:
    return pair in items


def append_pair_once(items: list[tuple[str, int]], pair: tuple[str, int]):
    if pair not in items:
        items.append(pair)


@app.post("/create/<user_id>")
def create_order(user_id: str):
    user_reply = send_get_request(f"{GATEWAY_URL}/payment/find_user/{user_id}")
    if user_reply.status_code != 200:
        abort(400, f"User: {user_id} not found!")

    key = str(uuid.uuid4())
    value = msgpack.encode(OrderValue(paid=False, items=[], user_id=user_id, total_cost=0))
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

    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(generate_entry()) for i in range(n)}
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
    if int(quantity) <= 0:
        abort(400, f"Quantity must be positive, got {quantity}")

    order_entry = get_order_from_db(order_id)
    item_reply = send_get_request(f"{GATEWAY_URL}/stock/find/{item_id}")
    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")

    item_json: dict = item_reply.json()
    order_entry.items.append((item_id, int(quantity)))
    order_entry.total_cost += int(quantity) * item_json["price"]
    if not persist_order(order_id, order_entry):
        abort(400, DB_ERROR_STR)
    return Response(
        f"Item: {item_id} added to: {order_id} price updated to: {order_entry.total_cost}",
        status=200,
    )


def aggregate_items(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
    item_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in items:
        item_quantities[item_id] += quantity
    return sorted(item_quantities.items(), key=lambda item: item[0])


def create_checkout_tx(order_entry: OrderValue, order_id: str, items_snapshot: list[tuple[str, int]]) -> CheckoutTxValue:
    now = now_ms()
    return CheckoutTxValue(
        tx_id=str(uuid.uuid4()),
        order_id=order_id,
        user_id=order_entry.user_id,
        total_cost=order_entry.total_cost,
        protocol=CHECKOUT_PROTOCOL,
        status=STATUS_INIT,
        items_snapshot=items_snapshot,
        stock_prepared=[],
        stock_committed=[],
        stock_compensated=[],
        payment_prepared=False,
        payment_committed=False,
        payment_reversed=False,
        decision=None,
        started_at_ms=now,
        updated_at_ms=now,
        last_error=None,
    )


def mark_tx_failed(tx_entry: CheckoutTxValue, message: str, best_effort: bool = False):
    tx_entry.status = STATUS_FAILED_NEEDS_RECOVERY
    tx_entry.last_error = message
    if best_effort:
        try_save_tx(tx_entry)
    else:
        save_tx(tx_entry)


def compensate_saga(tx_entry: CheckoutTxValue) -> bool:
    all_compensated = True

    if tx_entry.payment_committed and not tx_entry.payment_reversed:
        payment_refund = payment_internal_post(f"/internal/tx/saga/refund/{tx_entry.tx_id}")
        if is_ok_response(payment_refund):
            tx_entry.payment_reversed = True
            save_tx(tx_entry)
        else:
            all_compensated = False

    for item_id, quantity in reversed(tx_entry.stock_prepared):
        pair = (item_id, quantity)
        if contains_pair(tx_entry.stock_compensated, pair):
            continue
        stock_release = stock_internal_post(f"/internal/tx/saga/release/{tx_entry.tx_id}/{item_id}")
        if is_ok_response(stock_release):
            tx_entry.stock_compensated.append(pair)
            save_tx(tx_entry)
        else:
            all_compensated = False

    return all_compensated


def abort_2pc(tx_entry: CheckoutTxValue, user_message: str):
    tx_entry.decision = DECISION_ABORT
    tx_entry.status = STATUS_2PC_ABORTING
    tx_entry.last_error = user_message
    save_tx(tx_entry)

    all_aborted = True

    if tx_entry.payment_prepared and not tx_entry.payment_reversed:
        payment_abort = payment_internal_post(f"/internal/tx/abort_pay/{tx_entry.tx_id}")
        if is_ok_response(payment_abort):
            tx_entry.payment_reversed = True
            save_tx(tx_entry)
        else:
            all_aborted = False

    for item_id, quantity in reversed(tx_entry.stock_prepared):
        pair = (item_id, quantity)
        if contains_pair(tx_entry.stock_compensated, pair):
            continue
        stock_abort = stock_internal_post(f"/internal/tx/abort_subtract/{tx_entry.tx_id}/{item_id}")
        if is_ok_response(stock_abort):
            tx_entry.stock_compensated.append(pair)
            save_tx(tx_entry)
        else:
            all_aborted = False

    if all_aborted:
        tx_entry.status = STATUS_ABORTED
        tx_entry.last_error = user_message
        save_tx(tx_entry)
    else:
        mark_tx_failed(tx_entry, f"2PC abort incomplete: {user_message}")

    abort(400, user_message)


def run_saga_checkout(order_id: str, order_entry: OrderValue, tx_entry: CheckoutTxValue) -> Response:
    for item_id, quantity in tx_entry.items_snapshot:
        stock_reserve = stock_internal_post(f"/internal/tx/saga/reserve/{tx_entry.tx_id}/{item_id}/{quantity}")
        if not is_ok_response(stock_reserve):
            tx_entry.status = STATUS_SAGA_COMPENSATING
            tx_entry.last_error = f"Out of stock on item_id: {item_id}"
            save_tx(tx_entry)
            if compensate_saga(tx_entry):
                tx_entry.status = STATUS_ABORTED
                save_tx(tx_entry)
            else:
                mark_tx_failed(tx_entry, f"Saga stock compensation incomplete for item: {item_id}")
            abort(400, f"Out of stock on item_id: {item_id}")

        pair = (item_id, quantity)
        append_pair_once(tx_entry.stock_prepared, pair)
        append_pair_once(tx_entry.stock_committed, pair)
        tx_entry.status = STATUS_SAGA_STOCK_RESERVED
        tx_entry.last_error = None
        save_tx(tx_entry)

    payment_pay = payment_internal_post(
        f"/internal/tx/saga/pay/{tx_entry.tx_id}/{order_entry.user_id}/{order_entry.total_cost}"
    )
    if not is_ok_response(payment_pay):
        tx_entry.status = STATUS_SAGA_COMPENSATING
        tx_entry.last_error = "User out of credit"
        save_tx(tx_entry)
        if compensate_saga(tx_entry):
            tx_entry.status = STATUS_ABORTED
            save_tx(tx_entry)
        else:
            mark_tx_failed(tx_entry, "Saga compensation incomplete after payment failure")
        abort(400, "User out of credit")

    tx_entry.payment_committed = True
    tx_entry.status = STATUS_SAGA_PAYMENT_CHARGED
    tx_entry.last_error = None
    save_tx(tx_entry)

    order_entry.paid = True
    if not persist_order(order_id, order_entry):
        tx_entry.status = STATUS_SAGA_COMPENSATING
        tx_entry.last_error = "Order DB write failed while marking order paid"
        save_tx(tx_entry)
        if compensate_saga(tx_entry):
            tx_entry.status = STATUS_ABORTED
            save_tx(tx_entry)
        else:
            mark_tx_failed(tx_entry, "Saga compensation incomplete after order DB failure")
        abort(400, DB_ERROR_STR)

    tx_entry.status = STATUS_COMPLETED
    tx_entry.last_error = None
    save_tx(tx_entry)
    return Response("Checkout successful", status=200)


def run_2pc_checkout(order_id: str, order_entry: OrderValue, tx_entry: CheckoutTxValue) -> Response:
    tx_entry.status = STATUS_2PC_PREPARING
    save_tx(tx_entry)

    for item_id, quantity in tx_entry.items_snapshot:
        stock_prepare = stock_internal_post(f"/internal/tx/prepare_subtract/{tx_entry.tx_id}/{item_id}/{quantity}")
        if stock_prepare is None:
            # Timeout is ambiguous; branch may have prepared already.
            append_pair_once(tx_entry.stock_prepared, (item_id, quantity))
            save_tx(tx_entry)
            abort_2pc(tx_entry, REQ_ERROR_STR)
        if stock_prepare.status_code != 200:
            abort_2pc(tx_entry, f"Out of stock on item_id: {item_id}")

        append_pair_once(tx_entry.stock_prepared, (item_id, quantity))
        save_tx(tx_entry)

    payment_prepare = payment_internal_post(
        f"/internal/tx/prepare_pay/{tx_entry.tx_id}/{order_entry.user_id}/{order_entry.total_cost}"
    )
    if payment_prepare is None:
        # Timeout is ambiguous; payment may have prepared already.
        tx_entry.payment_prepared = True
        save_tx(tx_entry)
        abort_2pc(tx_entry, REQ_ERROR_STR)
    if payment_prepare.status_code != 200:
        abort_2pc(tx_entry, "User out of credit")

    tx_entry.payment_prepared = True
    tx_entry.status = STATUS_2PC_PREPARED
    tx_entry.last_error = None
    save_tx(tx_entry)

    tx_entry.decision = DECISION_COMMIT
    tx_entry.status = STATUS_2PC_COMMITTING
    save_tx(tx_entry)
    set_commit_fence(order_id, tx_entry.tx_id)

    for item_id, quantity in tx_entry.stock_prepared:
        stock_commit = stock_internal_post(f"/internal/tx/commit_subtract/{tx_entry.tx_id}/{item_id}")
        if stock_commit is None:
            mark_tx_failed(tx_entry, f"Stock commit request failed on item_id: {item_id}")
            abort(400, REQ_ERROR_STR)
        if stock_commit.status_code != 200:
            mark_tx_failed(tx_entry, f"Stock commit failed on item_id: {item_id}")
            abort(400, f"Stock commit failed on item_id: {item_id}")

        append_pair_once(tx_entry.stock_committed, (item_id, quantity))
        save_tx(tx_entry)

    payment_commit = payment_internal_post(f"/internal/tx/commit_pay/{tx_entry.tx_id}")
    if payment_commit is None:
        mark_tx_failed(tx_entry, "Payment commit request failed")
        abort(400, REQ_ERROR_STR)
    if payment_commit.status_code != 200:
        mark_tx_failed(tx_entry, "Payment commit failed")
        abort(400, "Payment commit failed")

    tx_entry.payment_committed = True
    save_tx(tx_entry)

    order_entry.paid = True
    if not persist_order(order_id, order_entry):
        tx_entry.status = STATUS_2PC_FINALIZATION_PENDING
        tx_entry.last_error = "Order DB write failed after 2PC commit"
        try_save_tx(tx_entry)
        abort(400, DB_ERROR_STR)

    clear_commit_fence(order_id)
    tx_entry.status = STATUS_COMPLETED
    tx_entry.last_error = None
    save_tx(tx_entry)
    return Response("Checkout successful", status=200)


def run_checkout_protocol(order_id: str, order_entry: OrderValue, tx_entry: CheckoutTxValue) -> Response:
    if CHECKOUT_PROTOCOL == "saga":
        return run_saga_checkout(order_id, order_entry, tx_entry)
    return run_2pc_checkout(order_id, order_entry, tx_entry)


def recover_fenced_2pc_commit(order_id: str, order_entry: OrderValue) -> str:
    tx_id = get_commit_fence_tx_id(order_id)
    if tx_id is None:
        return "cleared"

    tx_entry = get_tx(tx_id)
    if tx_entry is None or tx_entry.order_id != order_id or tx_entry.protocol != "2pc":
        clear_commit_fence(order_id)
        return "cleared"

    if tx_entry.decision != DECISION_COMMIT:
        clear_commit_fence(order_id)
        return "cleared"

    for item_id, quantity in tx_entry.items_snapshot:
        pair = (item_id, quantity)
        if contains_pair(tx_entry.stock_committed, pair):
            continue
        stock_commit = stock_internal_post(f"/internal/tx/commit_subtract/{tx_entry.tx_id}/{item_id}")
        if not is_ok_response(stock_commit):
            tx_entry.status = STATUS_FAILED_NEEDS_RECOVERY
            tx_entry.last_error = "Commit recovery blocked on stock participant"
            try_save_tx(tx_entry)
            return "blocked"
        append_pair_once(tx_entry.stock_committed, pair)
        try_save_tx(tx_entry)

    if not tx_entry.payment_committed:
        payment_commit = payment_internal_post(f"/internal/tx/commit_pay/{tx_entry.tx_id}")
        if not is_ok_response(payment_commit):
            tx_entry.status = STATUS_FAILED_NEEDS_RECOVERY
            tx_entry.last_error = "Commit recovery blocked on payment participant"
            try_save_tx(tx_entry)
            return "blocked"
        tx_entry.payment_committed = True
        try_save_tx(tx_entry)

    order_entry.paid = True
    if not persist_order(order_id, order_entry):
        tx_entry.status = STATUS_2PC_FINALIZATION_PENDING
        tx_entry.last_error = "Order DB write failed during fence recovery"
        try_save_tx(tx_entry)
        return "blocked"

    clear_commit_fence(order_id)
    tx_entry.status = STATUS_COMPLETED
    tx_entry.last_error = None
    try_save_tx(tx_entry)
    return "finalized"


@app.post("/checkout/<order_id>")
def checkout(order_id: str):
    app.logger.debug(f"Checking out {order_id} with protocol={CHECKOUT_PROTOCOL}")
    lock_token = acquire_checkout_lock(order_id)
    if lock_token is None:
        abort(409, f"Checkout already in progress for order: {order_id}")

    tx_entry: CheckoutTxValue | None = None
    try:
        order_entry = get_order_from_db(order_id)
        if not order_entry.paid and has_commit_fence(order_id):
            recovery_outcome = recover_fenced_2pc_commit(order_id, order_entry)
            if recovery_outcome == "finalized":
                mark_recent_checkout(order_id)
                return Response("Order finalized from pending 2PC commit", status=200)
            if recovery_outcome == "blocked":
                abort(409, f"Checkout finalization pending for order: {order_id}")
            order_entry = get_order_from_db(order_id)

        if order_entry.paid:
            if has_commit_fence(order_id):
                clear_commit_fence(order_id)
            if has_recent_checkout(order_id):
                abort(409, f"Checkout already completed for order: {order_id}")
            return Response("Order already paid", status=200)

        active_tx = get_active_tx(order_id)
        if active_tx and not is_terminal_status(active_tx.status):
            abort(409, f"Checkout already in progress for order: {order_id}")

        mark_recent_checkout(order_id)
        items_snapshot = aggregate_items(order_entry.items)
        tx_entry = create_checkout_tx(order_entry, order_id, items_snapshot)
        save_tx(tx_entry)
        set_active_tx(order_id, tx_entry.tx_id)

        return run_checkout_protocol(order_id, order_entry, tx_entry)
    finally:
        if tx_entry and is_terminal_status(tx_entry.status):
            clear_active_tx(order_id)
        release_checkout_lock(order_id, lock_token)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
