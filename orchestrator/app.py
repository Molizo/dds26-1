import atexit
import logging
import os
import uuid

import redis
import requests
from flask import Flask, Response, abort, jsonify, request

from common.constants import (
    ACTIVE_TX_GUARD_TTL,
    ORDER_MUTATION_GUARD_TTL,
    TERMINAL_STATUSES,
    VALID_PROTOCOLS,
)
from common.result import CheckoutResult
from order_port import HttpOrderPort
from tx_store import (
    acquire_active_tx_guard,
    acquire_mutation_guard,
    clear_active_tx_guard,
    get_active_tx_guard,
    get_decision,
    get_mutation_guard,
    get_tx,
    release_mutation_guard,
)

DB_ERROR_STR = "DB error"

CHECKOUT_PROTOCOL = os.environ.get("CHECKOUT_PROTOCOL", "").lower()
if CHECKOUT_PROTOCOL not in VALID_PROTOCOLS:
    raise RuntimeError(
        f"CHECKOUT_PROTOCOL must be one of {sorted(VALID_PROTOCOLS)}, "
        f"got: {CHECKOUT_PROTOCOL!r}"
    )

ORDER_SERVICE_URL = os.environ["ORDER_SERVICE_URL"]
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

app = Flask("orchestrator-service")

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


class _TxStoreImpl:
    """Redis-backed TxStorePort owned by the orchestrator."""

    def __getattr__(self, name):
        import tx_store

        fn = getattr(tx_store, name)

        def bound(*args, **kwargs):
            return fn(db, *args, **kwargs)

        setattr(self, name, bound)
        return bound


def _get_order_port() -> HttpOrderPort:
    if not hasattr(_get_order_port, "_instance"):
        _get_order_port._instance = HttpOrderPort(ORDER_SERVICE_URL, _session)
    return _get_order_port._instance


def _get_coordinator():
    if not hasattr(_get_coordinator, "_instance"):
        from coordinator.service import CoordinatorService

        _get_coordinator._instance = CoordinatorService(
            rabbitmq_url=RABBITMQ_URL,
            order_port=_get_order_port(),
            tx_store=_TxStoreImpl(),
        )
    return _get_coordinator._instance


def _map_checkout_result(result: CheckoutResult) -> Response:
    if result.success:
        return Response("Checkout successful", status=200)
    abort(result.status_code, result.error or "Checkout failed")


@app.post("/checkout/<order_id>")
def checkout(order_id: str):
    order_port = _get_order_port()
    snapshot = order_port.read_order(order_id)
    if snapshot is None:
        abort(400, f"Order: {order_id} not found!")
    if snapshot.paid:
        return Response("Checkout successful", status=200)

    tx_id = str(uuid.uuid4())
    acquired = acquire_active_tx_guard(db, order_id, tx_id, ACTIVE_TX_GUARD_TTL)

    if not acquired:
        existing_tx_id = get_active_tx_guard(db, order_id)
        if existing_tx_id:
            existing_tx = get_tx(db, existing_tx_id)
            if existing_tx and existing_tx.status in TERMINAL_STATUSES:
                clear_active_tx_guard(db, order_id)
                acquired = acquire_active_tx_guard(db, order_id, tx_id, ACTIVE_TX_GUARD_TTL)

        if not acquired:
            abort(409, "Checkout already in progress")

    coordinator = _get_coordinator()
    result: CheckoutResult = coordinator.execute_checkout(order_id, CHECKOUT_PROTOCOL, tx_id)

    tx_after = get_tx(db, tx_id)
    if tx_after is not None and tx_after.status in TERMINAL_STATUSES:
        clear_active_tx_guard(db, order_id)

    return _map_checkout_result(result)


@app.get("/internal/tx_decision/<tx_id>")
def tx_decision(tx_id: str):
    decision = get_decision(db, tx_id)
    if decision is None:
        return jsonify({"decision": "unknown"})
    return jsonify({"decision": decision})


@app.post("/internal/orders/<order_id>/mutation_guard/acquire")
def acquire_order_mutation_guard(order_id: str):
    payload = request.get_json(silent=True) or {}
    lease_id = payload.get("lease_id")
    if not lease_id:
        abort(400, "lease_id is required")

    try:
        acquired = acquire_mutation_guard(db, order_id, str(lease_id), ORDER_MUTATION_GUARD_TTL)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)

    if acquired:
        return jsonify({"acquired": True})

    reason = "busy"
    if get_active_tx_guard(db, order_id):
        reason = "checkout_in_progress"
    elif get_mutation_guard(db, order_id):
        reason = "mutation_in_progress"
    return jsonify({"acquired": False, "reason": reason}), 409


@app.post("/internal/orders/<order_id>/mutation_guard/release")
def release_order_mutation_guard(order_id: str):
    payload = request.get_json(silent=True) or {}
    lease_id = payload.get("lease_id")
    if not lease_id:
        abort(400, "lease_id is required")

    try:
        released = release_mutation_guard(db, order_id, str(lease_id))
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)

    return jsonify({"released": bool(released)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
