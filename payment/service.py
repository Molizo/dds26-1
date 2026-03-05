"""Atomic payment operations backed by Redis Lua scripts.

All operations are performed atomically — no GET-then-SET patterns.
"""
import json
import logging

import redis

from common.constants import PARTICIPANT_TX_TTL
import lua_scripts

logger = logging.getLogger(__name__)

_scripts: dict = {}


def _get_script(db: redis.Redis, name: str, source: str):
    if name not in _scripts:
        _scripts[name] = db.register_script(source)
    return _scripts[name]


# ---------------------------------------------------------------------------
# Public endpoint operations
# ---------------------------------------------------------------------------

def pay_credit(db: redis.Redis, user_id: str, amount: int) -> dict:
    """Atomically subtract credit. Returns {"ok": True, "credit": new_level}
    or {"ok": False, "error": "not_found"|"insufficient_credit"}."""
    script = _get_script(db, "pay", lua_scripts.PAY_CREDIT)
    try:
        new_credit = script(keys=[user_id], args=[amount])
        return {"ok": True, "credit": int(new_credit)}
    except redis.exceptions.ResponseError as exc:
        return {"ok": False, "error": str(exc)}


def add_credit(db: redis.Redis, user_id: str, amount: int) -> dict:
    """Atomically add credit. Returns {"ok": True, "credit": new_level}
    or {"ok": False, "error": "not_found"}."""
    script = _get_script(db, "add_credit", lua_scripts.ADD_CREDIT)
    try:
        new_credit = script(keys=[user_id], args=[amount])
        return {"ok": True, "credit": int(new_credit)}
    except redis.exceptions.ResponseError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Transaction operations
# ---------------------------------------------------------------------------

def hold_payment(db: redis.Redis, tx_id: str, user_id: str, amount: int) -> dict:
    """Atomically hold payment funds.

    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    script = _get_script(db, "hold", lua_scripts.PAYMENT_HOLD)
    try:
        raw = script(keys=[user_id], args=[tx_id, str(PARTICIPANT_TX_TTL), str(amount)])
        return json.loads(raw)
    except redis.exceptions.RedisError as exc:
        logger.error("hold_payment Redis error tx=%s: %s", tx_id, exc)
        return {"ok": False, "error": "redis_error"}


def release_payment(db: redis.Redis, tx_id: str, user_id: str, amount: int) -> dict:
    """Atomically release a payment hold (refund).

    Safe to call when the hold never happened — returns ok=True with
    error="tx_not_found" which the coordinator treats as a harmless no-op.
    """
    script = _get_script(db, "release", lua_scripts.PAYMENT_RELEASE)
    try:
        raw = script(keys=[user_id], args=[tx_id, str(PARTICIPANT_TX_TTL), str(amount)])
        return json.loads(raw)
    except redis.exceptions.RedisError as exc:
        logger.error("release_payment Redis error tx=%s: %s", tx_id, exc)
        return {"ok": False, "error": "redis_error"}


def commit_payment(db: redis.Redis, tx_id: str) -> dict:
    """Mark the payment tx record as committed. No credit change.

    Safe to call multiple times — idempotent by tx_id.
    """
    tx_key = f"payment_tx:{tx_id}"
    script = _get_script(db, "commit", lua_scripts.PAYMENT_COMMIT)
    try:
        raw = script(keys=[tx_key], args=[tx_id, str(PARTICIPANT_TX_TTL)])
        return json.loads(raw)
    except redis.exceptions.RedisError as exc:
        logger.error("commit_payment Redis error tx=%s: %s", tx_id, exc)
        return {"ok": False, "error": "redis_error"}
