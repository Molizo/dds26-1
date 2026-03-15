"""Atomic stock operations backed by Redis Lua scripts.

All public-endpoint operations (add, subtract) and transaction operations
(hold, release, commit) are performed atomically. No GET-then-SET patterns.

Functions return structured dicts rather than Flask responses so this module
has no Flask dependency and remains testable in isolation.
"""
import json
import logging

import redis

from common.constants import PARTICIPANT_TX_TTL
import lua_scripts

logger = logging.getLogger(__name__)

# Registered Lua scripts (sha-cached after first SCRIPT LOAD call)
_scripts: dict = {}


def _get_script(db: redis.Redis, name: str, source: str):
    """Return a cached Script object, registering it on first use."""
    if name not in _scripts:
        _scripts[name] = db.register_script(source)
    return _scripts[name]


# ---------------------------------------------------------------------------
# Public endpoint operations
# ---------------------------------------------------------------------------

def subtract_stock(db: redis.Redis, item_id: str, amount: int) -> dict:
    """Atomically subtract stock. Returns {"ok": True, "stock": new_level}
    or {"ok": False, "error": "not_found"|"insufficient_stock"}."""
    script = _get_script(db, "subtract", lua_scripts.SUBTRACT_STOCK)
    try:
        new_stock = script(keys=[item_id], args=[amount])
        return {"ok": True, "stock": int(new_stock)}
    except redis.exceptions.ResponseError as exc:
        return {"ok": False, "error": str(exc)}


def add_stock(db: redis.Redis, item_id: str, amount: int) -> dict:
    """Atomically add stock. Returns {"ok": True, "stock": new_level}
    or {"ok": False, "error": "not_found"}."""
    script = _get_script(db, "add", lua_scripts.ADD_STOCK)
    try:
        new_stock = script(keys=[item_id], args=[amount])
        return {"ok": True, "stock": int(new_stock)}
    except redis.exceptions.ResponseError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Transaction operations
# ---------------------------------------------------------------------------

def hold_stock(
    db: redis.Redis,
    tx_id: str,
    items: list[tuple[str, int]],
) -> dict:
    """Atomically hold stock for all items in a single Lua call.

    items: list of (item_id, quantity) pairs — pre-aggregated by coordinator.
    Returns {"ok": True} or {"ok": False, "error": "...", "item": "..."}.
    """
    if not items:
        return {"ok": True}  # nothing to hold

    keys = [item_id for item_id, _ in items]
    quantities = [str(qty) for _, qty in items]
    args = [tx_id, str(PARTICIPANT_TX_TTL)] + quantities

    script = _get_script(db, "hold", lua_scripts.STOCK_HOLD)
    try:
        raw = script(keys=keys, args=args)
        return json.loads(raw)
    except redis.exceptions.RedisError as exc:
        logger.error("hold_stock Redis error tx=%s: %s", tx_id, exc)
        return {"ok": False, "error": "redis_error"}


def release_stock(
    db: redis.Redis,
    tx_id: str,
    items: list[tuple[str, int]],
) -> dict:
    """Atomically release a stock hold. Returns {"ok": True} or error dict.

    Safe to call when the hold never happened — returns ok=True with
    error="tx_not_found" which the coordinator treats as a harmless no-op.
    """
    if not items:
        return {"ok": True}

    keys = [item_id for item_id, _ in items]
    quantities = [str(qty) for _, qty in items]
    args = [tx_id, str(PARTICIPANT_TX_TTL)] + quantities

    script = _get_script(db, "release", lua_scripts.STOCK_RELEASE)
    try:
        raw = script(keys=keys, args=args)
        return json.loads(raw)
    except redis.exceptions.RedisError as exc:
        logger.error("release_stock Redis error tx=%s: %s", tx_id, exc)
        return {"ok": False, "error": "redis_error"}


def commit_stock(db: redis.Redis, tx_id: str) -> dict:
    """Mark the stock tx record as committed. No stock change.

    Safe to call multiple times — idempotent by tx_id.
    """
    tx_key = f"stock_tx:{tx_id}"
    script = _get_script(db, "commit", lua_scripts.STOCK_COMMIT)
    try:
        raw = script(keys=[tx_key], args=[tx_id, str(PARTICIPANT_TX_TTL)])
        return json.loads(raw)
    except redis.exceptions.RedisError as exc:
        logger.error("commit_stock Redis error tx=%s: %s", tx_id, exc)
        return {"ok": False, "error": "redis_error"}
