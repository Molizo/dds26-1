import redis
from flask import abort
from msgspec import msgpack
import uuid

from common.redis_lock import acquire_lock, release_lock, renew_lock
from common.time_utils import now_ms
from config import (
    CHECKOUT_PROTOCOL,
    DB_ERROR_STR,
    ORDER_ACTIVE_TX_PREFIX,
    ORDER_COMMIT_FENCE_PREFIX,
    ORDER_LOCK_KEY_PREFIX,
    ORDER_LOCK_TTL_SECONDS,
    ORDER_RECENT_CHECKOUT_PREFIX,
    RECENT_CHECKOUT_TTL_SECONDS,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    STATUS_INIT,
    TERMINAL_TX_STATUSES,
    TX_KEY_PREFIX,
    STATUS_FAILED_NEEDS_RECOVERY,
)
from models import CheckoutTxValue, OrderValue

db: redis.Redis = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    db=REDIS_DB,
)


def close_db_connection():
    db.close()


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


def has_recent_checkout(order_id: str) -> bool:
    try:
        return bool(db.exists(recent_checkout_key(order_id)))
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


def acquire_checkout_lock(order_id: str) -> str | None:
    try:
        return acquire_lock(db, lock_key(order_id), ORDER_LOCK_TTL_SECONDS)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def release_checkout_lock(order_id: str, token: str):
    try:
        release_lock(db, lock_key(order_id), token)
    except redis.exceptions.RedisError:
        abort(400, DB_ERROR_STR)


def renew_checkout_lock(order_id: str, token: str) -> bool:
    try:
        return renew_lock(db, lock_key(order_id), token, ORDER_LOCK_TTL_SECONDS)
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


def aggregate_items(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
    item_quantities: dict[str, int] = {}
    for item_id, quantity in items:
        item_quantities[item_id] = item_quantities.get(item_id, 0) + quantity
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


def contains_pair(items: list[tuple[str, int]], pair: tuple[str, int]) -> bool:
    return pair in items


def append_pair_once(items: list[tuple[str, int]], pair: tuple[str, int]):
    if pair not in items:
        items.append(pair)
