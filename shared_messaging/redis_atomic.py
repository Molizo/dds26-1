from dataclasses import dataclass
from pathlib import Path
from typing import Final

import redis

from shared_messaging.redis_keys import (
    EFFECT_TTL_SEC,
    INBOX_TTL_SEC,
    effect_key,
    inbox_key,
    payment_hash_key,
    stock_hash_key,
)

_LUA_SCRIPTS: Final[dict[str, str]] = {
    "stock_reserve": "stock_reserve.lua",
    "stock_release": "stock_release.lua",
    "payment_charge": "payment_charge.lua",
    "payment_refund": "payment_refund.lua",
}

_SCRIPT_SHA_CACHE: dict[tuple[int, str], str] = {}
_SCRIPT_BODY_CACHE: dict[str, str] = {}

ATOMIC_APPLIED: Final[str] = "applied"
ATOMIC_DUPLICATE: Final[str] = "duplicate"
ATOMIC_BUSINESS_REJECT: Final[str] = "business_reject"
ATOMIC_MISSING_ENTITY: Final[str] = "missing_entity"


@dataclass(frozen=True)
class AtomicResult:
    status: str
    raw_code: int


def _script_path(script_name: str) -> Path:
    script_file = _LUA_SCRIPTS.get(script_name)
    if script_file is None:
        raise ValueError(f"Unknown Lua script name: {script_name}")
    return Path(__file__).resolve().parent / "lua" / script_file


def _script_body(script_name: str) -> str:
    cached = _SCRIPT_BODY_CACHE.get(script_name)
    if cached is not None:
        return cached
    body = _script_path(script_name).read_text(encoding="utf-8")
    _SCRIPT_BODY_CACHE[script_name] = body
    return body


def _script_cache_key(redis_client: redis.Redis, script_name: str) -> tuple[int, str]:
    return (id(redis_client.connection_pool), script_name)


def _load_script(redis_client: redis.Redis, script_name: str) -> str:
    sha = redis_client.script_load(_script_body(script_name))
    _SCRIPT_SHA_CACHE[_script_cache_key(redis_client, script_name)] = sha
    return sha


def _eval_script(
    redis_client: redis.Redis,
    script_name: str,
    keys: list[str],
    args: list[int],
) -> int:
    cache_key = _script_cache_key(redis_client, script_name)
    sha = _SCRIPT_SHA_CACHE.get(cache_key)
    if sha is None:
        sha = _load_script(redis_client, script_name)
    values = [*keys, *[str(value) for value in args]]
    try:
        result = redis_client.evalsha(sha, len(keys), *values)
    except redis.exceptions.NoScriptError:
        sha = _load_script(redis_client, script_name)
        result = redis_client.evalsha(sha, len(keys), *values)

    if not isinstance(result, int):
        raise ValueError(f"Unexpected Lua return type for {script_name}: {type(result).__name__}")
    return result


def _map_result(code: int) -> AtomicResult:
    if code == 1:
        return AtomicResult(status=ATOMIC_APPLIED, raw_code=code)
    if code == 0:
        return AtomicResult(status=ATOMIC_DUPLICATE, raw_code=code)
    if code == -1:
        return AtomicResult(status=ATOMIC_BUSINESS_REJECT, raw_code=code)
    if code == -2:
        return AtomicResult(status=ATOMIC_MISSING_ENTITY, raw_code=code)
    raise ValueError(f"Unexpected Lua return code: {code}")


def reserve_stock_atomic(
    redis_client: redis.Redis,
    *,
    service: str,
    message_id: str,
    item_id: str,
    step: str,
    quantity: int,
    inbox_ttl_sec: int = INBOX_TTL_SEC,
    effect_ttl_sec: int = EFFECT_TTL_SEC,
) -> AtomicResult:
    code = _eval_script(
        redis_client,
        "stock_reserve",
        keys=[
            stock_hash_key(item_id),
            inbox_key(service, message_id),
            effect_key(service, item_id, step),
        ],
        args=[int(quantity), int(inbox_ttl_sec), int(effect_ttl_sec)],
    )
    return _map_result(code)


def release_stock_atomic(
    redis_client: redis.Redis,
    *,
    service: str,
    message_id: str,
    item_id: str,
    step: str,
    quantity: int,
    inbox_ttl_sec: int = INBOX_TTL_SEC,
    effect_ttl_sec: int = EFFECT_TTL_SEC,
) -> AtomicResult:
    code = _eval_script(
        redis_client,
        "stock_release",
        keys=[
            stock_hash_key(item_id),
            inbox_key(service, message_id),
            effect_key(service, item_id, step),
        ],
        args=[int(quantity), int(inbox_ttl_sec), int(effect_ttl_sec)],
    )
    return _map_result(code)


def charge_payment_atomic(
    redis_client: redis.Redis,
    *,
    service: str,
    message_id: str,
    user_id: str,
    step: str,
    amount: int,
    inbox_ttl_sec: int = INBOX_TTL_SEC,
    effect_ttl_sec: int = EFFECT_TTL_SEC,
) -> AtomicResult:
    code = _eval_script(
        redis_client,
        "payment_charge",
        keys=[
            payment_hash_key(user_id),
            inbox_key(service, message_id),
            effect_key(service, user_id, step),
        ],
        args=[int(amount), int(inbox_ttl_sec), int(effect_ttl_sec)],
    )
    return _map_result(code)


def refund_payment_atomic(
    redis_client: redis.Redis,
    *,
    service: str,
    message_id: str,
    user_id: str,
    step: str,
    amount: int,
    inbox_ttl_sec: int = INBOX_TTL_SEC,
    effect_ttl_sec: int = EFFECT_TTL_SEC,
) -> AtomicResult:
    code = _eval_script(
        redis_client,
        "payment_refund",
        keys=[
            payment_hash_key(user_id),
            inbox_key(service, message_id),
            effect_key(service, user_id, step),
        ],
        args=[int(amount), int(inbox_ttl_sec), int(effect_ttl_sec)],
    )
    return _map_result(code)
