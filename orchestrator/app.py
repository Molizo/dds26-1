import atexit
import logging
import os

import redis
from flask import Flask, jsonify

from common.constants import (
    ACTIVE_TX_GUARD_TTL,
    ORDER_MUTATION_GUARD_TTL,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    TERMINAL_STATUSES,
    VALID_PROTOCOLS,
)
from common.result import CheckoutResult
import tx_store

try:
    from order_port import RabbitMqOrderPort
except ModuleNotFoundError:
    from orchestrator.order_port import RabbitMqOrderPort

DB_ERROR_STR = "DB error"

CHECKOUT_PROTOCOL = os.environ.get("CHECKOUT_PROTOCOL", "").lower()
if CHECKOUT_PROTOCOL not in VALID_PROTOCOLS:
    raise RuntimeError(
        f"CHECKOUT_PROTOCOL must be one of {sorted(VALID_PROTOCOLS)}, "
        f"got: {CHECKOUT_PROTOCOL!r}"
    )

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


class _TxStoreImpl:
    """Redis-backed TxStorePort owned by the orchestrator."""

    def create_tx(self, tx):
        tx_store.create_tx(db, tx)

    def update_tx(self, tx):
        tx_store.update_tx(db, tx)

    def get_tx(self, tx_id):
        return tx_store.get_tx(db, tx_id)

    def get_stale_non_terminal_txs(self, stale_before_ms, batch_limit=50):
        return tx_store.get_stale_non_terminal_txs(db, stale_before_ms, batch_limit)

    def set_decision(self, tx_id, decision):
        tx_store.set_decision(db, tx_id, decision)

    def set_decision_and_update_tx(self, tx_id, decision, tx):
        tx_store.set_decision_and_update_tx(db, tx_id, decision, tx)

    def get_decision(self, tx_id):
        return tx_store.get_decision(db, tx_id)

    def set_commit_fence(self, order_id, tx_id):
        tx_store.set_commit_fence(db, order_id, tx_id)

    def set_decision_fence_and_update_tx(self, tx_id, decision, order_id, tx):
        tx_store.set_decision_fence_and_update_tx(db, tx_id, decision, order_id, tx)

    def get_commit_fence(self, order_id):
        return tx_store.get_commit_fence(db, order_id)

    def clear_commit_fence(self, order_id):
        tx_store.clear_commit_fence(db, order_id)

    def acquire_active_tx_guard(self, order_id, tx_id, ttl):
        return tx_store.acquire_active_tx_guard(db, order_id, tx_id, ttl)

    def get_active_tx_guard(self, order_id):
        return tx_store.get_active_tx_guard(db, order_id)

    def clear_active_tx_guard(self, order_id):
        tx_store.clear_active_tx_guard(db, order_id)

    def clear_active_tx_guard_if_owned(self, order_id, tx_id):
        return tx_store.clear_active_tx_guard_if_owned(db, order_id, tx_id)

    def refresh_active_tx_guard(self, order_id, ttl):
        return tx_store.refresh_active_tx_guard(db, order_id, ttl)

    def acquire_mutation_guard(self, order_id, lease_id, ttl):
        return tx_store.acquire_mutation_guard(db, order_id, lease_id, ttl)

    def get_mutation_guard(self, order_id):
        return tx_store.get_mutation_guard(db, order_id)

    def release_mutation_guard(self, order_id, lease_id):
        return tx_store.release_mutation_guard(db, order_id, lease_id)

    def acquire_recovery_lock(self, tx_id, ttl):
        return tx_store.acquire_recovery_lock(db, tx_id, ttl)

    def release_recovery_lock(self, tx_id):
        tx_store.release_recovery_lock(db, tx_id)

    def acquire_recovery_leader(self, owner_id, ttl):
        return tx_store.acquire_recovery_leader(db, owner_id, ttl)

    def release_recovery_leader(self, owner_id):
        tx_store.release_recovery_leader(db, owner_id)


def _get_order_port() -> RabbitMqOrderPort:
    if not hasattr(_get_order_port, "_instance"):
        _get_order_port._instance = RabbitMqOrderPort(RABBITMQ_URL)
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


def _result_for_existing_tx(existing_tx) -> CheckoutResult:
    if existing_tx.status == STATUS_COMPLETED:
        return CheckoutResult.ok()
    if existing_tx.status == STATUS_ABORTED:
        return CheckoutResult.fail(existing_tx.last_error or "Checkout failed")
    return CheckoutResult.conflict()


def _maybe_clear_terminal_active_guard(order_id: str, tx_id: str) -> bool:
    existing_tx = tx_store.get_tx(db, tx_id)
    if existing_tx is None or existing_tx.status not in TERMINAL_STATUSES:
        return False
    return tx_store.clear_active_tx_guard_if_owned(db, order_id, tx_id)


def execute_checkout_command(order_id: str, tx_id: str) -> CheckoutResult:
    existing_tx = tx_store.get_tx(db, tx_id)
    if existing_tx is not None:
        return _result_for_existing_tx(existing_tx)

    order_port = _get_order_port()
    try:
        snapshot = order_port.read_order(order_id)
    except Exception:
        app.logger.exception("Order snapshot lookup failed order=%s tx=%s", order_id, tx_id)
        return CheckoutResult.fail("order_read_failed")
    if snapshot is None:
        return CheckoutResult.fail(f"Order: {order_id} not found!", code=400)
    if snapshot.paid:
        return CheckoutResult.paid()

    acquired = tx_store.acquire_active_tx_guard(db, order_id, tx_id, ACTIVE_TX_GUARD_TTL)
    if not acquired:
        existing_tx_id = tx_store.get_active_tx_guard(db, order_id)
        if existing_tx_id and _maybe_clear_terminal_active_guard(order_id, existing_tx_id):
            acquired = tx_store.acquire_active_tx_guard(db, order_id, tx_id, ACTIVE_TX_GUARD_TTL)
        if not acquired:
            return CheckoutResult.conflict()

    coordinator = _get_coordinator()
    result: CheckoutResult = coordinator.execute_checkout(order_id, CHECKOUT_PROTOCOL, tx_id)

    tx_after = tx_store.get_tx(db, tx_id)
    if tx_after is not None and tx_after.status in TERMINAL_STATUSES:
        tx_store.clear_active_tx_guard_if_owned(db, order_id, tx_id)

    return result


def acquire_mutation_guard_command(order_id: str, lease_id: str) -> tuple[bool, str | None, int]:
    try:
        acquired = tx_store.acquire_mutation_guard(db, order_id, lease_id, ORDER_MUTATION_GUARD_TTL)
    except redis.exceptions.RedisError:
        return False, DB_ERROR_STR, 400

    if acquired:
        return True, None, 200

    existing_tx_id = tx_store.get_active_tx_guard(db, order_id)
    if existing_tx_id and _maybe_clear_terminal_active_guard(order_id, existing_tx_id):
        try:
            reacquired = tx_store.acquire_mutation_guard(db, order_id, lease_id, ORDER_MUTATION_GUARD_TTL)
        except redis.exceptions.RedisError:
            return False, DB_ERROR_STR, 400
        if reacquired:
            return True, None, 200
        existing_tx_id = tx_store.get_active_tx_guard(db, order_id)

    if existing_tx_id:
        return False, "checkout_in_progress", 409
    if tx_store.get_mutation_guard(db, order_id):
        return False, "mutation_in_progress", 409
    return False, "busy", 409


def release_mutation_guard_command(order_id: str, lease_id: str) -> bool:
    try:
        return bool(tx_store.release_mutation_guard(db, order_id, lease_id))
    except redis.exceptions.RedisError:
        return False


@app.get("/internal/tx_decision/<tx_id>")
def tx_decision(tx_id: str):
    decision = tx_store.get_decision(db, tx_id)
    if decision is None:
        return jsonify({"decision": "unknown"})
    return jsonify({"decision": decision})


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
