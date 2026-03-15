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
    read_order_snapshot,
    mark_order_paid,
    get_decision,
    get_tx,
    acquire_active_tx_guard,
    get_active_tx_guard,
    clear_active_tx_guard,
)
from common.constants import (
    VALID_PROTOCOLS, TERMINAL_STATUSES, ACTIVE_TX_GUARD_TTL,
    STATUS_FAILED_NEEDS_RECOVERY,
)
from common.result import CheckoutResult

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"
REQUEST_TIMEOUT_SECONDS = 5.0
REQUEST_RETRY_COUNT = 3
ORDER_UPDATE_RETRY_COUNT = 10

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
RABBITMQ_URL = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@rabbitmq:5672/')

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
# Coordinator (lazy init — reply consumer starts in post_fork)
# ---------------------------------------------------------------------------

class _OrderPortImpl:
    """Phase 1 OrderPort: direct Redis."""
    def read_order(self, order_id):
        return read_order_snapshot(db, order_id)

    def mark_paid(self, order_id, tx_id):
        return mark_order_paid(db, order_id)


class _TxStoreImpl:
    """Phase 1 TxStorePort: direct Redis via store module."""
    def __getattr__(self, name):
        import store
        fn = getattr(store, name)
        # Bind db as first argument
        def bound(*args, **kwargs):
            return fn(db, *args, **kwargs)
        setattr(self, name, bound)
        return bound


def _get_coordinator():
    """Lazy-init coordinator service (can't create at import time because
    the reply consumer starts in post_fork)."""
    if not hasattr(_get_coordinator, '_instance'):
        from coordinator.service import CoordinatorService
        _get_coordinator._instance = CoordinatorService(
            rabbitmq_url=RABBITMQ_URL,
            order_port=_OrderPortImpl(),
            tx_store=_TxStoreImpl(),
        )
    return _get_coordinator._instance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _send_post(url: str) -> requests.Response:
    for attempt in range(REQUEST_RETRY_COUNT):
        try:
            return _session.post(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as exc:
            app.logger.warning(
                "POST %s attempt %s/%s failed: %s",
                url,
                attempt + 1,
                REQUEST_RETRY_COUNT,
                exc,
            )
    abort(400, REQ_ERROR_STR)


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
    quantity = _require_positive_int(quantity, "quantity")
    # Direct HTTP to stock service — no gateway hairpin
    item_reply = _send_get(f"{STOCK_SERVICE_URL}/find/{item_id}")
    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")
    item_json: dict = item_reply.json()
    price = int(item_json["price"])
    order_paid_key = f"order_paid:{order_id}"
    active_tx_key = f"order_active_tx:{order_id}"

    for _ in range(ORDER_UPDATE_RETRY_COUNT):
        try:
            with db.pipeline() as pipe:
                pipe.watch(order_id, order_paid_key, active_tx_key)
                raw_order = pipe.get(order_id)
                if raw_order is None:
                    pipe.unwatch()
                    abort(400, f"Order: {order_id} not found!")

                order = msgpack.decode(raw_order, type=OrderValue)
                if order.paid or pipe.exists(order_paid_key) == 1:
                    pipe.unwatch()
                    abort(409, "Order already paid")

                guard_raw = pipe.get(active_tx_key)
                clear_stale_terminal_guard = False
                if guard_raw:
                    guard_tx_id = (
                        guard_raw.decode()
                        if isinstance(guard_raw, bytes)
                        else str(guard_raw)
                    )
                    existing_tx = get_tx(db, guard_tx_id)
                    if existing_tx is None or existing_tx.status not in TERMINAL_STATUSES:
                        pipe.unwatch()
                        abort(409, "Checkout already in progress")
                    clear_stale_terminal_guard = True

                order.items.append((item_id, quantity))
                order.total_cost += quantity * price

                pipe.multi()
                if clear_stale_terminal_guard:
                    pipe.delete(active_tx_key)
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

    abort(409, "Conflict: could not update order")


# ---------------------------------------------------------------------------
# Checkout — thin adapter over CoordinatorService
# ---------------------------------------------------------------------------

@app.post('/checkout/<order_id>')
def checkout(order_id: str):
    app.logger.debug("Checking out %s", order_id)
    order = _get_order_or_abort(order_id)

    # Already-paid fast path: no tx record, no RabbitMQ, no side effects
    if order.paid:
        return Response("Checkout successful", status=200)

    # Generate tx_id upfront so the guard stores the real tx_id
    tx_id = str(uuid.uuid4())

    # Acquire active-tx guard (SET NX EX)
    acquired = acquire_active_tx_guard(db, order_id, tx_id, ACTIVE_TX_GUARD_TTL)

    if not acquired:
        # Guard held by another tx — check if it's terminal
        existing_tx_id = get_active_tx_guard(db, order_id)
        if existing_tx_id:
            existing_tx = get_tx(db, existing_tx_id)
            if existing_tx and existing_tx.status in TERMINAL_STATUSES:
                # Stale terminal guard — clean up and retry
                clear_active_tx_guard(db, order_id)
                acquired = acquire_active_tx_guard(
                    db, order_id, tx_id, ACTIVE_TX_GUARD_TTL
                )

        if not acquired:
            abort(409, "Checkout already in progress")

    # Run coordinator
    coordinator = _get_coordinator()
    result: CheckoutResult = coordinator.execute_checkout(order_id, CHECKOUT_PROTOCOL, tx_id)

    # Clear guard only after the durable tx record reaches a terminal status.
    # FAILED_NEEDS_RECOVERY returns 200/400 depending on phase, so HTTP status
    # alone is not sufficient to decide guard cleanup safely.
    tx_after = get_tx(db, tx_id)
    if tx_after is not None and tx_after.status in TERMINAL_STATUSES:
        clear_active_tx_guard(db, order_id)

    # Map result to HTTP
    if result.success:
        return Response("Checkout successful", status=200)
    else:
        abort(result.status_code, result.error or "Checkout failed")


# ---------------------------------------------------------------------------
# Internal routes (coordinator ↔ participants)
# ---------------------------------------------------------------------------

@app.get('/internal/tx_decision/<tx_id>')
def tx_decision(tx_id: str):
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
