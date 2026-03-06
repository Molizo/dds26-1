"""Order domain and coordinator transaction persistence.

All Redis keys used by the coordinator are defined here. In Phase 2, the
coordinator gets its own Redis instance and this module becomes the
implementation of TxStorePort inside the orchestrator.

Key layout:
  order:{order_id}                   — OrderValue (msgpack)
  tx:{tx_id}                         — CheckoutTxValue (msgpack)
  tx_decision:{tx_id}                — "commit" or "abort" (plain string)
  order_active_tx:{order_id}         — tx_id with TTL (plain string)
  order_commit_fence:{order_id}      — tx_id (plain string)
  tx_recovery_lock:{tx_id}           — best-effort recovery mutex with TTL
  tx_index                           — Redis SET of all tx_ids (for get_non_terminal_txs scan)
"""
import time
import logging
from typing import Optional

import redis
import msgspec
from msgspec import msgpack

from coordinator.models import CheckoutTxValue
from coordinator.ports import OrderSnapshot
from common.constants import ACTIVE_TX_GUARD_TTL, TERMINAL_STATUSES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Order domain
# ---------------------------------------------------------------------------

class OrderValue(msgspec.Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int


_order_encoder = msgspec.msgpack.Encoder()
_order_decoder = msgspec.msgpack.Decoder(OrderValue)


def get_order(db: redis.Redis, order_id: str) -> Optional[OrderValue]:
    """Return the raw OrderValue or None if not found."""
    try:
        raw = db.get(order_id)
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error reading order %s: %s", order_id, exc)
        return None
    if raw is None:
        return None
    return _order_decoder.decode(raw)


def read_order_snapshot(db: redis.Redis, order_id: str) -> Optional[OrderSnapshot]:
    """Return an OrderSnapshot for the coordinator, or None if not found."""
    order = get_order(db, order_id)
    if order is None:
        return None
    return OrderSnapshot(
        order_id=order_id,
        user_id=order.user_id,
        total_cost=order.total_cost,
        paid=order.paid,
        items=list(order.items),
    )


def mark_order_paid(db: redis.Redis, order_id: str) -> bool:
    """Idempotently mark order as paid. Returns True on success."""
    try:
        raw = db.get(order_id)
        if raw is None:
            return False
        order = _order_decoder.decode(raw)
        if order.paid:
            return True  # already paid — idempotent no-op
        order = OrderValue(
            paid=True,
            items=order.items,
            user_id=order.user_id,
            total_cost=order.total_cost,
        )
        db.set(order_id, _order_encoder.encode(order))
        return True
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error marking order paid %s: %s", order_id, exc)
        return False


# ---------------------------------------------------------------------------
# Transaction record persistence
# ---------------------------------------------------------------------------

_tx_encoder = msgspec.msgpack.Encoder()
_tx_decoder = msgspec.msgpack.Decoder(CheckoutTxValue)

_TX_INDEX_KEY = "tx_index"


def _tx_key(tx_id: str) -> str:
    return f"tx:{tx_id}"


def create_tx(db: redis.Redis, tx: CheckoutTxValue) -> None:
    pipe = db.pipeline(transaction=True)
    pipe.set(_tx_key(tx.tx_id), _tx_encoder.encode(tx))
    pipe.sadd(_TX_INDEX_KEY, tx.tx_id)
    pipe.execute()


def update_tx(db: redis.Redis, tx: CheckoutTxValue) -> None:
    tx = CheckoutTxValue(
        tx_id=tx.tx_id,
        order_id=tx.order_id,
        user_id=tx.user_id,
        total_cost=tx.total_cost,
        protocol=tx.protocol,
        status=tx.status,
        decision=tx.decision,
        items_snapshot=tx.items_snapshot,
        stock_held=tx.stock_held,
        stock_committed=tx.stock_committed,
        stock_released=tx.stock_released,
        payment_held=tx.payment_held,
        payment_committed=tx.payment_committed,
        payment_released=tx.payment_released,
        last_error=tx.last_error,
        created_at=tx.created_at,
        updated_at=int(time.time() * 1000),
        retry_count=tx.retry_count,
    )
    db.set(_tx_key(tx.tx_id), _tx_encoder.encode(tx))


def get_tx(db: redis.Redis, tx_id: str) -> Optional[CheckoutTxValue]:
    try:
        raw = db.get(_tx_key(tx_id))
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error reading tx %s: %s", tx_id, exc)
        return None
    if raw is None:
        return None
    return _tx_decoder.decode(raw)


def get_non_terminal_txs(db: redis.Redis) -> list[CheckoutTxValue]:
    """Return all transactions that have not yet reached a terminal state.

    Used by the recovery worker. Scans the tx_index set and filters by status.
    """
    try:
        tx_ids = db.smembers(_TX_INDEX_KEY)
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error reading tx_index: %s", exc)
        return []

    results = []
    for tx_id_bytes in tx_ids:
        tx_id = tx_id_bytes.decode() if isinstance(tx_id_bytes, bytes) else tx_id_bytes
        tx = get_tx(db, tx_id)
        if tx is None:
            continue
        if tx.status not in TERMINAL_STATUSES:
            results.append(tx)
        else:
            # Prune terminal txs from the index lazily to keep the set small
            try:
                db.srem(_TX_INDEX_KEY, tx_id)
            except redis.exceptions.RedisError:
                pass
    return results


# ---------------------------------------------------------------------------
# Durable decision marker
# ---------------------------------------------------------------------------

def set_decision(db: redis.Redis, tx_id: str, decision: str) -> None:
    """Persist the coordinator's commit/abort decision.

    This is the most critical write: it must be persisted before the status
    changes to COMMITTING so that recovery always finds the decision marker
    when re-examining an in-progress commit.
    """
    db.set(f"tx_decision:{tx_id}", decision)


def get_decision(db: redis.Redis, tx_id: str) -> Optional[str]:
    raw = db.get(f"tx_decision:{tx_id}")
    return raw.decode() if raw else None


# ---------------------------------------------------------------------------
# Commit fence
# ---------------------------------------------------------------------------

def set_commit_fence(db: redis.Redis, order_id: str, tx_id: str) -> None:
    """Set the commit fence for an order.

    The fence signals that a commit decision was made and commit commands were
    (or will be) published. If the coordinator crashes after writing the fence
    but before confirming all commits, recovery finds the fence and re-publishes
    commit commands.
    """
    db.set(f"order_commit_fence:{order_id}", tx_id)


def get_commit_fence(db: redis.Redis, order_id: str) -> Optional[str]:
    raw = db.get(f"order_commit_fence:{order_id}")
    return raw.decode() if raw else None


def clear_commit_fence(db: redis.Redis, order_id: str) -> None:
    db.delete(f"order_commit_fence:{order_id}")


# ---------------------------------------------------------------------------
# Active-tx guard (merged lease lock + active-tx guard)
# ---------------------------------------------------------------------------

def acquire_active_tx_guard(
    db: redis.Redis, order_id: str, tx_id: str, ttl: int = ACTIVE_TX_GUARD_TTL
) -> bool:
    """Atomically set the guard if not already set. Returns True on success.

    Uses SET NX EX for atomic acquisition. The NX flag prevents concurrent
    acquisition; the EX TTL ensures the guard expires if the coordinator
    crashes without cleaning up (defense-in-depth — recovery also handles it).
    """
    result = db.set(
        f"order_active_tx:{order_id}",
        tx_id,
        nx=True,
        ex=ttl,
    )
    return result is True


def get_active_tx_guard(db: redis.Redis, order_id: str) -> Optional[str]:
    raw = db.get(f"order_active_tx:{order_id}")
    return raw.decode() if raw else None


def clear_active_tx_guard(db: redis.Redis, order_id: str) -> None:
    db.delete(f"order_active_tx:{order_id}")


def refresh_active_tx_guard(
    db: redis.Redis, order_id: str, ttl: int = ACTIVE_TX_GUARD_TTL
) -> bool:
    """Reset the TTL on an existing guard. Returns True if the key existed."""
    result = db.expire(f"order_active_tx:{order_id}", ttl)
    return bool(result)


# ---------------------------------------------------------------------------
# Recovery scan lock (best-effort, per tx_id)
# ---------------------------------------------------------------------------

def acquire_recovery_lock(
    db: redis.Redis, tx_id: str, ttl: int = ACTIVE_TX_GUARD_TTL
) -> bool:
    """Acquire a short-lived lock so only one worker resumes a tx at a time."""
    try:
        result = db.set(f"tx_recovery_lock:{tx_id}", "1", nx=True, ex=ttl)
        return result is True
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error acquiring recovery lock tx=%s: %s", tx_id, exc)
        return False


def release_recovery_lock(db: redis.Redis, tx_id: str) -> None:
    try:
        db.delete(f"tx_recovery_lock:{tx_id}")
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error releasing recovery lock tx=%s: %s", tx_id, exc)
