"""Redis-backed transaction store for the standalone orchestrator."""
import logging
import time
from typing import Optional

import msgspec
import redis
from redis.commands.core import Script

from common.constants import ACTIVE_TX_GUARD_TTL, TERMINAL_STATUSES
from orchestrator.models import CheckoutTxValue

logger = logging.getLogger(__name__)

_tx_encoder = msgspec.msgpack.Encoder()
_tx_decoder = msgspec.msgpack.Decoder(CheckoutTxValue)

_TX_UPDATED_AT_KEY = "tx_updated_at"
_RECOVERY_LEADER_KEY = "recovery_leader"

_acquire_active_tx_guard_script: Optional[Script] = None
_acquire_mutation_guard_script: Optional[Script] = None
_release_owned_key_script: Optional[Script] = None
_acquire_recovery_leader_script: Optional[Script] = None


def _tx_key(tx_id: str) -> str:
    return f"tx:{tx_id}"


def _active_tx_key(order_id: str) -> str:
    return f"order_active_tx:{order_id}"


def _mutation_guard_key(order_id: str) -> str:
    return f"order_mutation_guard:{order_id}"


def _stamp_and_encode(tx: CheckoutTxValue) -> tuple[bytes, int]:
    now_ms = int(time.time() * 1000)
    tx.updated_at = now_ms
    return _tx_encoder.encode(tx), now_ms


def _get_acquire_active_tx_guard_script(db: redis.Redis) -> Script:
    global _acquire_active_tx_guard_script
    if _acquire_active_tx_guard_script is None:
        _acquire_active_tx_guard_script = db.register_script(
            """
            local active_key = KEYS[1]
            local mutation_key = KEYS[2]
            local tx_id = ARGV[1]
            local ttl = tonumber(ARGV[2])
            local current = redis.call('GET', active_key)

            if current then
                if current == tx_id then
                    redis.call('EXPIRE', active_key, ttl)
                    return 1
                end
                return 0
            end
            if redis.call('EXISTS', mutation_key) == 1 then
                return 0
            end

            redis.call('SET', active_key, tx_id, 'EX', ttl)
            return 1
            """
        )
    return _acquire_active_tx_guard_script


def _get_acquire_mutation_guard_script(db: redis.Redis) -> Script:
    global _acquire_mutation_guard_script
    if _acquire_mutation_guard_script is None:
        _acquire_mutation_guard_script = db.register_script(
            """
            local mutation_key = KEYS[1]
            local active_key = KEYS[2]
            local lease_id = ARGV[1]
            local ttl = tonumber(ARGV[2])
            local current = redis.call('GET', mutation_key)

            if redis.call('EXISTS', active_key) == 1 then
                return 0
            end
            if current then
                if current == lease_id then
                    redis.call('EXPIRE', mutation_key, ttl)
                    return 1
                end
                return 0
            end

            redis.call('SET', mutation_key, lease_id, 'EX', ttl)
            return 1
            """
        )
    return _acquire_mutation_guard_script


def _get_release_owned_key_script(db: redis.Redis) -> Script:
    global _release_owned_key_script
    if _release_owned_key_script is None:
        _release_owned_key_script = db.register_script(
            """
            local key = KEYS[1]
            local owner = ARGV[1]
            local current = redis.call('GET', key)

            if not current then
                return 1
            end
            if current ~= owner then
                return 0
            end

            redis.call('DEL', key)
            return 1
            """
        )
    return _release_owned_key_script


def _get_acquire_recovery_leader_script(db: redis.Redis) -> Script:
    global _acquire_recovery_leader_script
    if _acquire_recovery_leader_script is None:
        _acquire_recovery_leader_script = db.register_script(
            """
            local key = KEYS[1]
            local owner = ARGV[1]
            local ttl = tonumber(ARGV[2])
            local current = redis.call('GET', key)

            if not current then
                redis.call('SET', key, owner, 'EX', ttl)
                return 1
            end
            if current == owner then
                redis.call('EXPIRE', key, ttl)
                return 1
            end
            return 0
            """
        )
    return _acquire_recovery_leader_script


def create_tx(db: redis.Redis, tx: CheckoutTxValue) -> None:
    pipe = db.pipeline(transaction=True)
    pipe.set(_tx_key(tx.tx_id), _tx_encoder.encode(tx))
    pipe.zadd(_TX_UPDATED_AT_KEY, {tx.tx_id: tx.updated_at})
    pipe.execute()


def update_tx(db: redis.Redis, tx: CheckoutTxValue) -> None:
    encoded, now_ms = _stamp_and_encode(tx)
    pipe = db.pipeline(transaction=False)
    pipe.set(_tx_key(tx.tx_id), encoded)
    pipe.zadd(_TX_UPDATED_AT_KEY, {tx.tx_id: now_ms})
    pipe.execute()


def get_tx(db: redis.Redis, tx_id: str) -> Optional[CheckoutTxValue]:
    try:
        raw = db.get(_tx_key(tx_id))
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error reading tx %s: %s", tx_id, exc)
        return None
    if raw is None:
        return None
    return _tx_decoder.decode(raw)


def get_stale_non_terminal_txs(
    db: redis.Redis,
    stale_before_ms: int,
    batch_limit: int = 50,
) -> list[CheckoutTxValue]:
    try:
        tx_ids_raw = db.zrangebyscore(
            _TX_UPDATED_AT_KEY,
            "-inf",
            stale_before_ms,
            start=0,
            num=batch_limit,
        )
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error reading tx_updated_at: %s", exc)
        return []

    results: list[CheckoutTxValue] = []
    for tx_id_bytes in tx_ids_raw:
        tx_id = tx_id_bytes.decode() if isinstance(tx_id_bytes, bytes) else tx_id_bytes
        tx = get_tx(db, tx_id)
        if tx is None:
            continue
        if tx.status not in TERMINAL_STATUSES:
            results.append(tx)
        else:
            try:
                db.zrem(_TX_UPDATED_AT_KEY, tx_id)
            except redis.exceptions.RedisError:
                pass
    return results


def set_decision(db: redis.Redis, tx_id: str, decision: str) -> None:
    db.set(f"tx_decision:{tx_id}", decision)


def set_decision_and_update_tx(
    db: redis.Redis, tx_id: str, decision: str, tx: CheckoutTxValue,
) -> None:
    encoded, now_ms = _stamp_and_encode(tx)
    pipe = db.pipeline(transaction=False)
    pipe.set(f"tx_decision:{tx_id}", decision)
    pipe.set(_tx_key(tx_id), encoded)
    pipe.zadd(_TX_UPDATED_AT_KEY, {tx_id: now_ms})
    pipe.execute()


def get_decision(db: redis.Redis, tx_id: str) -> Optional[str]:
    raw = db.get(f"tx_decision:{tx_id}")
    return raw.decode() if raw else None


def set_commit_fence(db: redis.Redis, order_id: str, tx_id: str) -> None:
    db.set(f"order_commit_fence:{order_id}", tx_id)


def set_decision_fence_and_update_tx(
    db: redis.Redis, tx_id: str, decision: str,
    order_id: str, tx: CheckoutTxValue,
) -> None:
    encoded, now_ms = _stamp_and_encode(tx)
    pipe = db.pipeline(transaction=False)
    pipe.set(f"tx_decision:{tx_id}", decision)
    pipe.set(f"order_commit_fence:{order_id}", tx_id)
    pipe.set(_tx_key(tx_id), encoded)
    pipe.zadd(_TX_UPDATED_AT_KEY, {tx_id: now_ms})
    pipe.execute()


def get_commit_fence(db: redis.Redis, order_id: str) -> Optional[str]:
    raw = db.get(f"order_commit_fence:{order_id}")
    return raw.decode() if raw else None


def clear_commit_fence(db: redis.Redis, order_id: str) -> None:
    db.delete(f"order_commit_fence:{order_id}")


def acquire_active_tx_guard(
    db: redis.Redis,
    order_id: str,
    tx_id: str,
    ttl: int = ACTIVE_TX_GUARD_TTL,
) -> bool:
    script = _get_acquire_active_tx_guard_script(db)
    result = script(keys=[_active_tx_key(order_id), _mutation_guard_key(order_id)], args=[tx_id, ttl])
    return int(result) == 1


def get_active_tx_guard(db: redis.Redis, order_id: str) -> Optional[str]:
    raw = db.get(_active_tx_key(order_id))
    return raw.decode() if raw else None


def clear_active_tx_guard(db: redis.Redis, order_id: str) -> None:
    db.delete(_active_tx_key(order_id))


def clear_active_tx_guard_if_owned(db: redis.Redis, order_id: str, tx_id: str) -> bool:
    script = _get_release_owned_key_script(db)
    result = script(keys=[_active_tx_key(order_id)], args=[tx_id])
    return int(result) == 1


def refresh_active_tx_guard(
    db: redis.Redis, order_id: str, ttl: int = ACTIVE_TX_GUARD_TTL,
) -> bool:
    result = db.expire(_active_tx_key(order_id), ttl)
    return bool(result)


def acquire_mutation_guard(
    db: redis.Redis,
    order_id: str,
    lease_id: str,
    ttl: int,
) -> bool:
    script = _get_acquire_mutation_guard_script(db)
    result = script(keys=[_mutation_guard_key(order_id), _active_tx_key(order_id)], args=[lease_id, ttl])
    return int(result) == 1


def get_mutation_guard(db: redis.Redis, order_id: str) -> Optional[str]:
    raw = db.get(_mutation_guard_key(order_id))
    return raw.decode() if raw else None


def release_mutation_guard(db: redis.Redis, order_id: str, lease_id: str) -> bool:
    script = _get_release_owned_key_script(db)
    result = script(keys=[_mutation_guard_key(order_id)], args=[lease_id])
    return int(result) == 1


def acquire_recovery_lock(
    db: redis.Redis, tx_id: str, ttl: int = ACTIVE_TX_GUARD_TTL,
) -> bool:
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


def acquire_recovery_leader(db: redis.Redis, owner_id: str, ttl: int) -> bool:
    try:
        script = _get_acquire_recovery_leader_script(db)
        result = script(keys=[_RECOVERY_LEADER_KEY], args=[owner_id, ttl])
        return int(result) == 1
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error acquiring recovery leader owner=%s: %s", owner_id, exc)
        return False


def release_recovery_leader(db: redis.Redis, owner_id: str) -> None:
    try:
        script = _get_release_owned_key_script(db)
        script(keys=[_RECOVERY_LEADER_KEY], args=[owner_id])
    except redis.exceptions.RedisError as exc:
        logger.error("Redis error releasing recovery leader owner=%s: %s", owner_id, exc)
