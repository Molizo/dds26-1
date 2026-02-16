import logging
import os
import sys
import time
import uuid
from pathlib import Path

import pika
import redis

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.append(str(SERVICE_ROOT))

try:
    from order.messaging import (
        build_rabbitmq_parameters,
        encode_internal_message,
        ensure_rabbitmq_topology,
    )
    from order.models import (
        ORDER_STATUS_COMPLETED,
        SAGA_STATE_CHARGING_PAYMENT,
        SAGA_STATE_COMMITTING_ORDER,
        SAGA_STATE_PENDING,
        SAGA_STATE_RELEASING_STOCK,
        SAGA_STATE_COMPLETED,
        SAGA_STATE_RESERVING_STOCK,
        SAGA_TERMINAL_STATES,
        OrderValue,
        SagaValue,
        decode_order,
        decode_saga,
        encode_order,
        encode_saga,
        now_ms,
    )
except ImportError:
    from messaging import (
        build_rabbitmq_parameters,
        encode_internal_message,
        ensure_rabbitmq_topology,
    )
    from models import (
        ORDER_STATUS_COMPLETED,
        SAGA_STATE_CHARGING_PAYMENT,
        SAGA_STATE_COMMITTING_ORDER,
        SAGA_STATE_PENDING,
        SAGA_STATE_RELEASING_STOCK,
        SAGA_STATE_COMPLETED,
        SAGA_STATE_RESERVING_STOCK,
        SAGA_TERMINAL_STATES,
        OrderValue,
        SagaValue,
        decode_order,
        decode_saga,
        encode_order,
        encode_saga,
        now_ms,
    )
from shared_messaging.contracts import (
    ChargePaymentPayload,
    CheckoutRequestedPayload,
    MessageMetadata,
    ReleaseStockPayload,
    ReserveStockPayload,
)
from shared_messaging.redis_keys import recovery_leader_lock_key, recovery_step_lock_key

LOGGER = logging.getLogger("reconciliation-worker")
db: redis.Redis = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    password=os.environ.get("REDIS_PASSWORD", "redis"),
    db=int(os.environ.get("REDIS_DB", "0")),
)

_RENEW_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
  return 0
end
"""

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"""


def _get_int_env(name: str, default: int, *, min_value: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        LOGGER.warning("Invalid %s=%s; using default=%s", name, raw, default)
        return default
    if parsed < min_value:
        LOGGER.warning("Out-of-range %s=%s; using default=%s", name, raw, default)
        return default
    return parsed


def _saga_key(saga_id: str) -> str:
    return f"saga:{saga_id}"


def _recovery_message_id(saga_id: str, step: str) -> str:
    return f"recovery:{saga_id}:{step}"


def _decode_text(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def _acquire_leader_lock(redis_client: redis.Redis, owner_token: str, ttl_ms: int) -> bool:
    try:
        return bool(
            redis_client.set(
                recovery_leader_lock_key(),
                owner_token,
                nx=True,
                px=ttl_ms,
            )
        )
    except redis.exceptions.RedisError:
        LOGGER.exception("Failed to acquire reconciliation leader lock")
        return False


def _renew_leader_lock(redis_client: redis.Redis, owner_token: str, ttl_ms: int) -> bool:
    try:
        refreshed = redis_client.eval(
            _RENEW_LOCK_LUA,
            1,
            recovery_leader_lock_key(),
            owner_token,
            str(ttl_ms),
        )
    except redis.exceptions.RedisError:
        LOGGER.exception("Failed to renew reconciliation leader lock")
        return False
    return int(refreshed) == 1


def _release_leader_lock(redis_client: redis.Redis, owner_token: str) -> None:
    try:
        redis_client.eval(_RELEASE_LOCK_LUA, 1, recovery_leader_lock_key(), owner_token)
    except redis.exceptions.RedisError:
        LOGGER.warning("Failed to release reconciliation leader lock")


def _is_stale(saga_entry: SagaValue, *, stale_after_ms: int, current_ms: int) -> bool:
    return current_ms - int(saga_entry.updated_at_ms) >= stale_after_ms


def _build_recovery_metadata(saga_entry: SagaValue, *, step: str) -> MessageMetadata:
    message_id = _recovery_message_id(saga_entry.saga_id, step)
    return MessageMetadata(
        message_id=message_id,
        saga_id=saga_entry.saga_id,
        order_id=saga_entry.order_id,
        step=step,
        attempt=0,
        timestamp=now_ms(),
        correlation_id=saga_entry.saga_id,
        causation_id=message_id,
    )


def _publish_internal_message(
    channel,
    *,
    exchange: str,
    routing_key: str,
    message_type: str,
    metadata: MessageMetadata,
    payload: object,
):
    body = encode_internal_message(message_type, metadata, payload)
    channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=body,
        properties=pika.BasicProperties(
            content_type="application/json",
            correlation_id=metadata.correlation_id,
            delivery_mode=2,
            message_id=metadata.message_id,
            timestamp=int(time.time()),
            type=message_type,
            headers={
                "x-dds-recovery": True,
                "x-dds-recovery-step": metadata.step,
            },
        ),
        mandatory=False,
    )


def _touch_saga(redis_client: redis.Redis, saga_entry: SagaValue) -> None:
    saga_entry.updated_at_ms = now_ms()
    redis_client.set(_saga_key(saga_entry.saga_id), encode_saga(saga_entry))


def _load_order(redis_client: redis.Redis, order_id: str) -> OrderValue | None:
    return decode_order(redis_client.get(order_id))


def _save_order(redis_client: redis.Redis, order_id: str, order_entry: OrderValue) -> None:
    redis_client.set(order_id, encode_order(order_entry))


def _complete_committing_order(redis_client: redis.Redis, saga_entry: SagaValue) -> None:
    order_entry = _load_order(redis_client, saga_entry.order_id)
    if order_entry is not None:
        order_entry.paid = True
        order_entry.checkout_status = ORDER_STATUS_COMPLETED
        order_entry.checkout_failure_reason = None
        _save_order(redis_client, saga_entry.order_id, order_entry)
    saga_entry.state = SAGA_STATE_COMPLETED
    saga_entry.failure_reason = None
    _touch_saga(redis_client, saga_entry)


def _publish_for_state(channel, saga_entry: SagaValue, *, step: str) -> None:
    metadata = _build_recovery_metadata(saga_entry, step=step)
    if saga_entry.state == SAGA_STATE_PENDING:
        _publish_internal_message(
            channel,
            exchange="checkout.command",
            routing_key="checkout.command",
            message_type="CheckoutRequested",
            metadata=metadata,
            payload=CheckoutRequestedPayload(
                user_id=saga_entry.user_id,
                total_cost=saga_entry.total_cost,
                items=saga_entry.items,
            ),
        )
        return
    if saga_entry.state == SAGA_STATE_RESERVING_STOCK:
        _publish_internal_message(
            channel,
            exchange="stock.command",
            routing_key="stock.command",
            message_type="ReserveStock",
            metadata=metadata,
            payload=ReserveStockPayload(items=saga_entry.items),
        )
        return
    if saga_entry.state == SAGA_STATE_CHARGING_PAYMENT:
        _publish_internal_message(
            channel,
            exchange="payment.command",
            routing_key="payment.command",
            message_type="ChargePayment",
            metadata=metadata,
            payload=ChargePaymentPayload(
                user_id=saga_entry.user_id,
                amount=saga_entry.total_cost,
            ),
        )
        return
    if saga_entry.state == SAGA_STATE_RELEASING_STOCK:
        _publish_internal_message(
            channel,
            exchange="stock.command",
            routing_key="stock.command",
            message_type="ReleaseStock",
            metadata=metadata,
            payload=ReleaseStockPayload(items=saga_entry.items),
        )
        return
    raise ValueError(f"Unsupported reconciliation state={saga_entry.state}")


def _target_step_for_state(state: str) -> str | None:
    if state == SAGA_STATE_PENDING:
        return "recover_checkout_requested"
    if state == SAGA_STATE_RESERVING_STOCK:
        return "recover_reserve_stock"
    if state == SAGA_STATE_CHARGING_PAYMENT:
        return "recover_charge_payment"
    if state == SAGA_STATE_RELEASING_STOCK:
        return "recover_release_stock"
    if state == SAGA_STATE_COMMITTING_ORDER:
        return "recover_commit_order"
    return None


def _iter_sagas(redis_client: redis.Redis, *, batch_size: int):
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor=cursor, match="saga:*", count=batch_size)
        for key in keys:
            saga_entry = decode_saga(redis_client.get(key))
            if saga_entry is not None:
                yield saga_entry
        if cursor == 0:
            break


def reconcile_once(
    redis_client: redis.Redis,
    channel,
    *,
    stale_after_ms: int,
    batch_size: int,
    step_lock_ttl_sec: int,
    max_actions: int,
) -> dict[str, int]:
    stats = {
        "scanned": 0,
        "stale": 0,
        "reconciled": 0,
        "locked_out": 0,
        "errors": 0,
    }
    if max_actions <= 0:
        return stats

    current_ms = now_ms()
    for saga_entry in _iter_sagas(redis_client, batch_size=batch_size):
        stats["scanned"] += 1
        if saga_entry.state in SAGA_TERMINAL_STATES:
            continue
        if not _is_stale(saga_entry, stale_after_ms=stale_after_ms, current_ms=current_ms):
            continue
        stats["stale"] += 1
        target_step = _target_step_for_state(saga_entry.state)
        if target_step is None:
            continue

        step_lock = recovery_step_lock_key(saga_entry.saga_id, target_step)
        try:
            lock_acquired = bool(redis_client.set(step_lock, "1", nx=True, ex=step_lock_ttl_sec))
        except redis.exceptions.RedisError:
            stats["errors"] += 1
            LOGGER.exception(
                "Failed to acquire step lock for saga=%s step=%s",
                saga_entry.saga_id,
                target_step,
            )
            continue
        if not lock_acquired:
            stats["locked_out"] += 1
            continue

        try:
            if saga_entry.state == SAGA_STATE_COMMITTING_ORDER:
                _complete_committing_order(redis_client, saga_entry)
            else:
                _publish_for_state(channel, saga_entry, step=target_step)
                _touch_saga(redis_client, saga_entry)
            stats["reconciled"] += 1
        except Exception:
            stats["errors"] += 1
            LOGGER.exception(
                "Failed reconciling saga=%s state=%s step=%s",
                saga_entry.saga_id,
                saga_entry.state,
                target_step,
            )

        if stats["reconciled"] >= max_actions:
            break
    return stats


def run_startup_recovery_once(
    channel,
    redis_client: redis.Redis,
    *,
    stale_after_ms: int,
    batch_size: int,
    step_lock_ttl_sec: int,
    max_actions: int,
    leader_lock_ttl_ms: int,
    owner_token: str | None = None,
) -> dict[str, int] | None:
    token = owner_token or f"startup-{uuid.uuid4()}"
    if not _acquire_leader_lock(redis_client, token, leader_lock_ttl_ms):
        return None
    try:
        return reconcile_once(
            redis_client,
            channel,
            stale_after_ms=stale_after_ms,
            batch_size=batch_size,
            step_lock_ttl_sec=step_lock_ttl_sec,
            max_actions=max_actions,
        )
    finally:
        _release_leader_lock(redis_client, token)


def run_worker():
    scan_interval_ms = _get_int_env("RECOVERY_SCAN_INTERVAL_MS", 5000, min_value=100)
    stale_after_ms = _get_int_env("RECOVERY_STALE_AFTER_MS", 15000, min_value=1000)
    batch_size = _get_int_env("RECOVERY_BATCH_SIZE", 200, min_value=1)
    step_lock_ttl_sec = _get_int_env("RECOVERY_STEP_LOCK_TTL_SEC", 120, min_value=1)
    max_actions = _get_int_env("RECOVERY_MAX_ACTIONS_PER_CYCLE", 500, min_value=1)
    leader_lock_ttl_ms = _get_int_env("RECOVERY_LEADER_LOCK_TTL_MS", 10000, min_value=1000)
    reconnect_backoff_ms = _get_int_env("WORKER_RECONNECT_BACKOFF_MS", 2000, min_value=100)
    owner_token = f"recovery-worker-{uuid.uuid4()}"
    lock_held = False

    while True:
        connection = None
        channel = None
        try:
            connection = pika.BlockingConnection(build_rabbitmq_parameters())
            channel = connection.channel()
            ensure_rabbitmq_topology(channel)
            LOGGER.info("Reconciliation worker started")
            while True:
                if not lock_held:
                    lock_held = _acquire_leader_lock(db, owner_token, leader_lock_ttl_ms)
                    if lock_held:
                        LOGGER.info("Acquired reconciliation leader lock")
                    else:
                        time.sleep(scan_interval_ms / 1000)
                        continue

                if not _renew_leader_lock(db, owner_token, leader_lock_ttl_ms):
                    lock_held = False
                    LOGGER.info("Lost reconciliation leader lock")
                    time.sleep(scan_interval_ms / 1000)
                    continue

                stats = reconcile_once(
                    db,
                    channel,
                    stale_after_ms=stale_after_ms,
                    batch_size=batch_size,
                    step_lock_ttl_sec=step_lock_ttl_sec,
                    max_actions=max_actions,
                )
                LOGGER.info(
                    "Reconciliation cycle stats scanned=%s stale=%s reconciled=%s locked_out=%s errors=%s",
                    stats["scanned"],
                    stats["stale"],
                    stats["reconciled"],
                    stats["locked_out"],
                    stats["errors"],
                )
                time.sleep(scan_interval_ms / 1000)
        except KeyboardInterrupt:
            LOGGER.info("Reconciliation worker shutdown requested")
            break
        except Exception:
            LOGGER.exception(
                "Reconciliation worker failed; reconnecting in %sms",
                reconnect_backoff_ms,
            )
            time.sleep(reconnect_backoff_ms / 1000)
        finally:
            if lock_held:
                _release_leader_lock(db, owner_token)
                lock_held = False
            if channel is not None and channel.is_open:
                channel.close()
            if connection is not None and connection.is_open:
                connection.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_worker()
