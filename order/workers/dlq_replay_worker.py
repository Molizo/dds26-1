import hashlib
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
    from order.messaging import build_rabbitmq_parameters, ensure_rabbitmq_topology
except ImportError:
    from messaging import build_rabbitmq_parameters, ensure_rabbitmq_topology
from shared_messaging.codec import decode_message
from shared_messaging.errors import MessageContractError
from shared_messaging.redis_keys import (
    dlq_replay_attempt_key,
    dlq_replay_leader_lock_key,
)

LOGGER = logging.getLogger("dlq-replay-worker")
db: redis.Redis = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    password=os.environ.get("REDIS_PASSWORD", "redis"),
    db=int(os.environ.get("REDIS_DB", "0")),
)

SOURCE_QUEUE_DESTINATION: dict[str, tuple[str, str]] = {
    "checkout.command.q": ("checkout.command", "checkout.command"),
    "stock.command.q": ("stock.command", "stock.command"),
    "payment.command.q": ("payment.command", "payment.command"),
    "order.command.q": ("order.command", "order.command"),
}

DLQ_SOURCE_FALLBACK: dict[str, str] = {
    "checkout.command.dlq": "checkout.command.q",
    "stock.command.dlq": "stock.command.q",
    "payment.command.dlq": "payment.command.q",
    "order.command.dlq": "order.command.q",
}

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


def _decode_text(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def _header_value(headers: object, key: str) -> str | None:
    if not isinstance(headers, dict):
        return None
    for header_key, header_value in headers.items():
        header_name = _decode_text(header_key)
        if header_name == key:
            return _decode_text(header_value)
    return None


def _source_queue_from_headers(headers: object, dlq_queue_name: str) -> str | None:
    source_queue = _header_value(headers, "x-dds-source-queue")
    if source_queue is not None:
        return source_queue
    return DLQ_SOURCE_FALLBACK.get(dlq_queue_name)


def _derive_original_message_id(body: bytes, properties) -> str:
    if properties is not None:
        property_id = _decode_text(getattr(properties, "message_id", None))
        if property_id:
            return property_id
    try:
        decoded = decode_message(body)
        if decoded.metadata.message_id:
            return decoded.metadata.message_id
    except MessageContractError:
        pass
    digest = hashlib.sha256(body).hexdigest()
    return f"body-hash:{digest}"


def _replay_message_id(original_message_id: str) -> str:
    return f"dlq-replay:{original_message_id}"


def _throttle(state: dict[str, float], rate_per_sec: int) -> None:
    if rate_per_sec <= 0:
        return
    minimum_interval = 1.0 / float(rate_per_sec)
    now = time.monotonic()
    previous = state.get("last_publish_at", 0.0)
    wait_for = minimum_interval - (now - previous)
    if wait_for > 0:
        time.sleep(wait_for)
    state["last_publish_at"] = time.monotonic()


def _acquire_leader_lock(redis_client: redis.Redis, owner_token: str, ttl_ms: int) -> bool:
    try:
        return bool(redis_client.set(dlq_replay_leader_lock_key(), owner_token, nx=True, px=ttl_ms))
    except redis.exceptions.RedisError:
        LOGGER.exception("Failed to acquire DLQ replay leader lock")
        return False


def _renew_leader_lock(redis_client: redis.Redis, owner_token: str, ttl_ms: int) -> bool:
    try:
        refreshed = redis_client.eval(
            _RENEW_LOCK_LUA,
            1,
            dlq_replay_leader_lock_key(),
            owner_token,
            str(ttl_ms),
        )
    except redis.exceptions.RedisError:
        LOGGER.exception("Failed to renew DLQ replay leader lock")
        return False
    return int(refreshed) == 1


def _release_leader_lock(redis_client: redis.Redis, owner_token: str) -> None:
    try:
        redis_client.eval(_RELEASE_LOCK_LUA, 1, dlq_replay_leader_lock_key(), owner_token)
    except redis.exceptions.RedisError:
        LOGGER.warning("Failed to release DLQ replay leader lock")


def _publish_with_headers(
    channel,
    *,
    exchange: str,
    routing_key: str,
    body: bytes,
    properties,
    headers: dict[str, object],
    message_id: str,
):
    publish_properties = pika.BasicProperties(
        app_id=getattr(properties, "app_id", None),
        content_encoding=getattr(properties, "content_encoding", None),
        content_type=getattr(properties, "content_type", None),
        correlation_id=getattr(properties, "correlation_id", None),
        delivery_mode=2,
        headers=headers,
        message_id=message_id,
        timestamp=int(time.time()),
        type=getattr(properties, "type", None),
    )
    channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=body,
        properties=publish_properties,
        mandatory=False,
    )


def _read_attempt_count(redis_client: redis.Redis, original_message_id: str) -> int:
    key = dlq_replay_attempt_key(original_message_id)
    raw_count = redis_client.get(key)
    if raw_count is None:
        return 0
    text = _decode_text(raw_count)
    if text is None:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _increment_attempt_count(
    redis_client: redis.Redis,
    *,
    original_message_id: str,
    ttl_sec: int,
) -> None:
    key = dlq_replay_attempt_key(original_message_id)
    attempts = redis_client.incr(key)
    if attempts == 1:
        redis_client.expire(key, ttl_sec)


def _publish_to_parking(
    channel,
    *,
    body: bytes,
    properties,
    original_message_id: str,
    source_queue: str | None,
    reason: str,
) -> None:
    headers = {}
    if properties is not None and isinstance(properties.headers, dict):
        headers.update(properties.headers)
    headers["x-dds-dlq-parking"] = True
    headers["x-dds-dlq-parking-reason"] = reason
    headers["x-dds-dlq-original-id"] = original_message_id
    if source_queue is not None:
        headers["x-dds-source-queue"] = source_queue
    _publish_with_headers(
        channel,
        exchange="dlq.parking",
        routing_key="dlq.parking",
        body=body,
        properties=properties,
        headers=headers,
        message_id=f"dlq-parking:{original_message_id}",
    )


def _publish_replay(
    channel,
    *,
    body: bytes,
    properties,
    original_message_id: str,
    source_queue: str,
    exchange: str,
    routing_key: str,
) -> None:
    headers = {}
    if properties is not None and isinstance(properties.headers, dict):
        headers.update(properties.headers)
    headers["x-dds-dlq-replay"] = True
    headers["x-dds-dlq-original-id"] = original_message_id
    headers["x-dds-source-queue"] = source_queue
    _publish_with_headers(
        channel,
        exchange=exchange,
        routing_key=routing_key,
        body=body,
        properties=properties,
        headers=headers,
        message_id=_replay_message_id(original_message_id),
    )


def _handle_one_dlq_message(
    channel,
    redis_client: redis.Redis,
    *,
    dlq_queue_name: str,
    method,
    properties,
    body: bytes,
    max_attempts: int,
    attempt_ttl_sec: int,
    rate_limit_state: dict[str, float],
    replay_rate_per_sec: int,
) -> None:
    source_queue = _source_queue_from_headers(getattr(properties, "headers", None), dlq_queue_name)
    original_message_id = _derive_original_message_id(body, properties)

    destination = SOURCE_QUEUE_DESTINATION.get(source_queue or "")
    if destination is None:
        _throttle(rate_limit_state, replay_rate_per_sec)
        _publish_to_parking(
            channel,
            body=body,
            properties=properties,
            original_message_id=original_message_id,
            source_queue=source_queue,
            reason="invalid_source_queue",
        )
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    try:
        attempts = _read_attempt_count(redis_client, original_message_id)
    except redis.exceptions.RedisError:
        LOGGER.exception("Failed to read replay attempt count for message=%s", original_message_id)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        return

    if attempts >= max_attempts:
        _throttle(rate_limit_state, replay_rate_per_sec)
        _publish_to_parking(
            channel,
            body=body,
            properties=properties,
            original_message_id=original_message_id,
            source_queue=source_queue,
            reason="attempts_exhausted",
        )
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    exchange, routing_key = destination
    _throttle(rate_limit_state, replay_rate_per_sec)
    _publish_replay(
        channel,
        body=body,
        properties=properties,
        original_message_id=original_message_id,
        source_queue=source_queue or "",
        exchange=exchange,
        routing_key=routing_key,
    )
    try:
        _increment_attempt_count(
            redis_client,
            original_message_id=original_message_id,
            ttl_sec=attempt_ttl_sec,
        )
    except redis.exceptions.RedisError:
        LOGGER.exception(
            "Replay publish succeeded but attempt accounting failed for message=%s",
            original_message_id,
        )
    channel.basic_ack(delivery_tag=method.delivery_tag)


def run_worker():
    queue_names = [
        name.strip()
        for name in os.environ.get(
            "DLQ_REPLAY_QUEUES",
            "checkout.command.dlq,stock.command.dlq,payment.command.dlq,order.command.dlq",
        ).split(",")
        if name.strip()
    ]
    poll_interval_ms = _get_int_env("DLQ_REPLAY_POLL_INTERVAL_MS", 500, min_value=50)
    replay_rate_per_sec = _get_int_env("DLQ_REPLAY_RATE_PER_SEC", 20, min_value=1)
    max_attempts = _get_int_env("DLQ_REPLAY_MAX_ATTEMPTS", 3, min_value=1)
    attempt_ttl_sec = _get_int_env("DLQ_REPLAY_ATTEMPT_TTL_SEC", 24 * 60 * 60, min_value=1)
    leader_lock_ttl_ms = _get_int_env("DLQ_REPLAY_LEADER_LOCK_TTL_MS", 10000, min_value=1000)
    reconnect_backoff_ms = _get_int_env("WORKER_RECONNECT_BACKOFF_MS", 2000, min_value=100)
    owner_token = f"dlq-replay-worker-{uuid.uuid4()}"
    rate_limit_state: dict[str, float] = {"last_publish_at": 0.0}
    lock_held = False

    while True:
        connection = None
        channel = None
        try:
            connection = pika.BlockingConnection(build_rabbitmq_parameters())
            channel = connection.channel()
            ensure_rabbitmq_topology(channel)
            LOGGER.info("DLQ replay worker started queues=%s", ",".join(queue_names))
            while True:
                if not lock_held:
                    lock_held = _acquire_leader_lock(db, owner_token, leader_lock_ttl_ms)
                    if lock_held:
                        LOGGER.info("Acquired DLQ replay leader lock")
                    else:
                        time.sleep(poll_interval_ms / 1000)
                        continue

                if not _renew_leader_lock(db, owner_token, leader_lock_ttl_ms):
                    lock_held = False
                    LOGGER.info("Lost DLQ replay leader lock")
                    time.sleep(poll_interval_ms / 1000)
                    continue

                any_message = False
                for queue_name in queue_names:
                    method, properties, body = channel.basic_get(queue=queue_name, auto_ack=False)
                    if method is None:
                        continue
                    any_message = True
                    try:
                        _handle_one_dlq_message(
                            channel,
                            db,
                            dlq_queue_name=queue_name,
                            method=method,
                            properties=properties,
                            body=body,
                            max_attempts=max_attempts,
                            attempt_ttl_sec=attempt_ttl_sec,
                            rate_limit_state=rate_limit_state,
                            replay_rate_per_sec=replay_rate_per_sec,
                        )
                    except Exception:
                        LOGGER.exception("DLQ replay message handling failed; requeueing delivery")
                        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                if not any_message:
                    time.sleep(poll_interval_ms / 1000)
        except KeyboardInterrupt:
            LOGGER.info("DLQ replay worker shutdown requested")
            break
        except Exception:
            LOGGER.exception(
                "DLQ replay worker failed; reconnecting in %sms",
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
