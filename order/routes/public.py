import random
import time
import uuid

import redis
from flask import Blueprint, Response, abort, current_app, jsonify
from msgspec import msgpack

from clients import send_get_request
from config import (
    CHECKOUT_PROTOCOL,
    DB_ERROR_STR,
    GATEWAY_URL,
    REQUEST_HEALING_BUDGET_SECONDS,
    REQUEST_HEALING_RETRY_INTERVAL_SECONDS,
)
from flows.fence_recovery import recover_fenced_2pc_commit
from flows.saga import run_saga_checkout
from flows.two_pc import run_2pc_checkout
from models import CheckoutTxValue, OrderValue
from store import (
    acquire_checkout_lock,
    aggregate_items,
    clear_active_tx,
    clear_commit_fence,
    create_checkout_tx,
    db,
    get_active_tx,
    get_order_from_db,
    has_commit_fence,
    is_terminal_status,
    mark_recent_checkout,
    release_checkout_lock,
    renew_checkout_lock,
    save_tx,
    set_active_tx,
)

public_bp = Blueprint("order_public", __name__)


def run_checkout_protocol(order_id: str, order_entry: OrderValue, tx_entry: CheckoutTxValue) -> Response:
    if CHECKOUT_PROTOCOL == "saga":
        return run_saga_checkout(order_id, order_entry, tx_entry)
    return run_2pc_checkout(order_id, order_entry, tx_entry)


@public_bp.post("/create/<user_id>")
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


@public_bp.post("/batch_init/<n>/<n_items>/<n_users>/<item_price>")
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


@public_bp.get("/find/<order_id>")
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


@public_bp.post("/addItem/<order_id>/<item_id>/<quantity>")
def add_item(order_id: str, item_id: str, quantity: int):
    if int(quantity) <= 0:
        abort(400, f"Quantity must be positive, got {quantity}")

    from store import persist_order

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


@public_bp.post("/checkout/<order_id>")
def checkout(order_id: str):
    current_app.logger.debug(f"Checking out {order_id} with protocol={CHECKOUT_PROTOCOL}")
    lock_token = acquire_checkout_lock(order_id)
    if lock_token is None:
        abort(409, f"Checkout already in progress for order: {order_id}")

    tx_entry: CheckoutTxValue | None = None
    try:
        def ensure_checkout_lock_held():
            if not renew_checkout_lock(order_id, lock_token):
                abort(409, f"Checkout already in progress for order: {order_id}")

        order_entry = get_order_from_db(order_id)
        if not order_entry.paid and has_commit_fence(order_id):
            deadline = time.monotonic() + REQUEST_HEALING_BUDGET_SECONDS
            ensure_checkout_lock_held()
            recovery_outcome = recover_fenced_2pc_commit(order_id, order_entry)
            while recovery_outcome == "blocked" and time.monotonic() < deadline:
                ensure_checkout_lock_held()
                time.sleep(REQUEST_HEALING_RETRY_INTERVAL_SECONDS)
                ensure_checkout_lock_held()
                order_entry = get_order_from_db(order_id)
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
