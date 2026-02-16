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
        decide_message_action,
        encode_internal_message,
        ensure_rabbitmq_topology,
        get_message_logger,
    )
    from order.models import (
        ORDER_STATUS_COMPLETED,
        ORDER_STATUS_FAILED,
        SAGA_STATE_CHARGING_PAYMENT,
        SAGA_STATE_COMMITTING_ORDER,
        SAGA_STATE_COMPLETED,
        SAGA_STATE_FAILED,
        SAGA_STATE_PENDING,
        SAGA_STATE_RELEASING_STOCK,
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
    from order.workers.reconciliation_worker import run_startup_recovery_once
except ImportError:
    from messaging import (
        build_rabbitmq_parameters,
        decide_message_action,
        encode_internal_message,
        ensure_rabbitmq_topology,
        get_message_logger,
    )
    from models import (
        ORDER_STATUS_COMPLETED,
        ORDER_STATUS_FAILED,
        SAGA_STATE_CHARGING_PAYMENT,
        SAGA_STATE_COMMITTING_ORDER,
        SAGA_STATE_COMPLETED,
        SAGA_STATE_FAILED,
        SAGA_STATE_PENDING,
        SAGA_STATE_RELEASING_STOCK,
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
    from workers.reconciliation_worker import run_startup_recovery_once
from shared_messaging.consumer import RETRY_DESTINATION_RETRY, decide_retry_destination
from shared_messaging.contracts import (
    ChargePaymentPayload,
    CheckoutRequestedPayload,
    OrderCommittedPayload,
    MessageMetadata,
    OrderFailedPayload,
    PaymentRejectedPayload,
    ReleaseStockPayload,
    ReserveStockPayload,
    StockRejectedPayload,
    StockReservedPayload,
)
from shared_messaging.idempotency import PROCESS_ACTION_RETRY, process_idempotent_step
from shared_messaging.redis_atomic import ATOMIC_APPLIED, ATOMIC_DUPLICATE, AtomicResult
from shared_messaging.redis_keys import EFFECT_TTL_SEC, INBOX_TTL_SEC, effect_key, inbox_key

LOGGER = logging.getLogger("order-worker")
db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
)


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


def _get_queue_names() -> list[str]:
    configured = os.environ.get("WORKER_QUEUES")
    if configured is not None:
        queue_names = [name.strip() for name in configured.split(",") if name.strip()]
        if queue_names:
            return queue_names
    fallback = os.environ.get("RABBITMQ_QUEUE", "order.command.q")
    return [fallback]


def _decode_str(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    return raw.decode("utf-8")


def _saga_key(saga_id: str) -> str:
    return f"saga:{saga_id}"


def _order_saga_key(order_id: str) -> str:
    return f"order_saga:{order_id}"


def _load_saga(saga_id: str) -> SagaValue | None:
    return decode_saga(db.get(_saga_key(saga_id)))


def _save_saga(saga_entry: SagaValue) -> None:
    saga_entry.updated_at_ms = now_ms()
    db.set(_saga_key(saga_entry.saga_id), encode_saga(saga_entry))


def _load_order(order_id: str) -> OrderValue | None:
    return decode_order(db.get(order_id))


def _save_order(order_id: str, order_entry: OrderValue) -> None:
    db.set(order_id, encode_order(order_entry))


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
        ),
        mandatory=False,
    )


def _build_next_metadata(source: MessageMetadata, *, step: str) -> MessageMetadata:
    return MessageMetadata(
        message_id=str(uuid.uuid4()),
        saga_id=source.saga_id,
        order_id=source.order_id,
        step=step,
        attempt=0,
        timestamp=now_ms(),
        correlation_id=source.correlation_id,
        causation_id=source.message_id,
    )


def _publish_order_terminal_event(channel, *, source: MessageMetadata, success: bool, reason: str | None = None):
    if success:
        payload = OrderCommittedPayload()
        message_type = "OrderCommitted"
        routing_key = "order.committed"
    else:
        payload = OrderFailedPayload(reason=reason or "checkout_failed")
        message_type = "OrderFailed"
        routing_key = "order.failed"

    metadata = _build_next_metadata(source, step="order_terminal")
    if success:
        _publish_internal_message(
            channel,
            exchange="checkout.events",
            routing_key=routing_key,
            message_type=message_type,
            metadata=metadata,
            payload=payload,
        )
        return

    _publish_internal_message(
        channel,
        exchange="checkout.events",
        routing_key=routing_key,
        message_type=message_type,
        metadata=metadata,
        payload=payload,
    )


def _mark_order_failed(order_id: str, reason: str) -> None:
    order_entry = _load_order(order_id)
    if order_entry is None:
        return
    order_entry.checkout_status = ORDER_STATUS_FAILED
    order_entry.checkout_failure_reason = reason
    _save_order(order_id, order_entry)


def _mark_order_completed(order_id: str) -> None:
    order_entry = _load_order(order_id)
    if order_entry is None:
        return
    order_entry.paid = True
    order_entry.checkout_status = ORDER_STATUS_COMPLETED
    order_entry.checkout_failure_reason = None
    _save_order(order_id, order_entry)


def _fail_saga(saga_entry: SagaValue, *, reason: str) -> None:
    saga_entry.state = SAGA_STATE_FAILED
    saga_entry.failure_reason = reason
    _save_saga(saga_entry)
    _mark_order_failed(saga_entry.order_id, reason)


def _handle_checkout_requested(channel, message) -> None:
    payload = message.payload
    saga_entry = _load_saga(message.metadata.saga_id)
    if saga_entry is None or saga_entry.state in SAGA_TERMINAL_STATES:
        return

    if saga_entry.state == SAGA_STATE_PENDING:
        saga_entry.state = SAGA_STATE_RESERVING_STOCK
        _save_saga(saga_entry)

    if saga_entry.state != SAGA_STATE_RESERVING_STOCK:
        return

    items = saga_entry.items
    if isinstance(payload, CheckoutRequestedPayload):
        items = payload.items

    metadata = _build_next_metadata(message.metadata, step="reserve_stock")
    _publish_internal_message(
        channel,
        exchange="stock.command",
        routing_key="stock.command",
        message_type="ReserveStock",
        metadata=metadata,
        payload=ReserveStockPayload(items=items),
    )


def _handle_stock_reserved(channel, message) -> None:
    payload = message.payload
    if not isinstance(payload, StockReservedPayload):
        raise ValueError("Invalid payload for StockReserved")

    saga_entry = _load_saga(message.metadata.saga_id)
    if saga_entry is None or saga_entry.state in SAGA_TERMINAL_STATES:
        return

    if saga_entry.state == SAGA_STATE_RESERVING_STOCK:
        saga_entry.state = SAGA_STATE_CHARGING_PAYMENT
        _save_saga(saga_entry)

    if saga_entry.state != SAGA_STATE_CHARGING_PAYMENT:
        return

    metadata = _build_next_metadata(message.metadata, step="charge_payment")
    _publish_internal_message(
        channel,
        exchange="payment.command",
        routing_key="payment.command",
        message_type="ChargePayment",
        metadata=metadata,
        payload=ChargePaymentPayload(user_id=saga_entry.user_id, amount=saga_entry.total_cost),
    )


def _handle_stock_rejected(channel, message) -> None:
    payload = message.payload
    if not isinstance(payload, StockRejectedPayload):
        raise ValueError("Invalid payload for StockRejected")

    saga_entry = _load_saga(message.metadata.saga_id)
    if saga_entry is None:
        return

    if saga_entry.state in SAGA_TERMINAL_STATES:
        return

    _fail_saga(saga_entry, reason=payload.reason or "stock_rejected")
    _publish_order_terminal_event(channel, source=message.metadata, success=False, reason=payload.reason)


def _handle_payment_charged(channel, message) -> None:
    saga_entry = _load_saga(message.metadata.saga_id)
    if saga_entry is None or saga_entry.state in SAGA_TERMINAL_STATES:
        return

    if saga_entry.state == SAGA_STATE_CHARGING_PAYMENT:
        saga_entry.state = SAGA_STATE_COMMITTING_ORDER
        _save_saga(saga_entry)

    if saga_entry.state != SAGA_STATE_COMMITTING_ORDER:
        return

    _mark_order_completed(saga_entry.order_id)
    saga_entry.state = SAGA_STATE_COMPLETED
    saga_entry.failure_reason = None
    _save_saga(saga_entry)
    _publish_order_terminal_event(channel, source=message.metadata, success=True)


def _handle_payment_rejected(channel, message) -> None:
    payload = message.payload
    if not isinstance(payload, PaymentRejectedPayload):
        raise ValueError("Invalid payload for PaymentRejected")

    saga_entry = _load_saga(message.metadata.saga_id)
    if saga_entry is None or saga_entry.state in SAGA_TERMINAL_STATES:
        return

    if saga_entry.state == SAGA_STATE_CHARGING_PAYMENT:
        saga_entry.state = SAGA_STATE_RELEASING_STOCK
        _save_saga(saga_entry)

    if saga_entry.state != SAGA_STATE_RELEASING_STOCK:
        return

    metadata = _build_next_metadata(message.metadata, step="release_stock")
    _publish_internal_message(
        channel,
        exchange="stock.command",
        routing_key="stock.command",
        message_type="ReleaseStock",
        metadata=metadata,
        payload=ReleaseStockPayload(items=saga_entry.items),
    )


def _handle_stock_released(channel, message) -> None:
    saga_entry = _load_saga(message.metadata.saga_id)
    if saga_entry is None or saga_entry.state in SAGA_TERMINAL_STATES:
        return
    _fail_saga(saga_entry, reason="payment_rejected")
    _publish_order_terminal_event(channel, source=message.metadata, success=False, reason="payment_rejected")


def _dispatch_message(channel, message):
    message_type = message.message_type
    if message_type == "CheckoutRequested":
        _handle_checkout_requested(channel, message)
        return
    if message_type == "StockReserved":
        _handle_stock_reserved(channel, message)
        return
    if message_type == "StockRejected":
        _handle_stock_rejected(channel, message)
        return
    if message_type == "PaymentCharged":
        _handle_payment_charged(channel, message)
        return
    if message_type == "PaymentRejected":
        _handle_payment_rejected(channel, message)
        return
    if message_type == "StockReleased":
        _handle_stock_released(channel, message)
        return
    if message_type in ("OrderCommitted", "OrderFailed"):
        return
    raise ValueError(f"Unsupported order worker message type={message_type}")


def mark_order_message_processed(message_id: str, order_id: str, step: str) -> AtomicResult:
    marker_key = inbox_key("order-worker", message_id)
    applied = db.set(marker_key, "1", ex=INBOX_TTL_SEC, nx=True)
    if not applied:
        return AtomicResult(status=ATOMIC_DUPLICATE, raw_code=0)
    db.set(effect_key("order-worker", order_id, step), "1", ex=EFFECT_TTL_SEC)
    return AtomicResult(status=ATOMIC_APPLIED, raw_code=1)


def process_delivery(channel, body: bytes) -> str:
    decision = decide_message_action(body)
    if decision.action != "ack" or decision.message is None:
        LOGGER.warning("Rejected message: %s", decision.reason)
        return "reject"

    message = decision.message
    message_logger = get_message_logger(LOGGER, message.metadata)
    message_logger.info("Validated message type=%s", message.message_type)

    outcome = process_idempotent_step(
        apply_effect=lambda: mark_order_message_processed(
            message.metadata.message_id,
            message.metadata.order_id,
            f"{message.metadata.step}:{message.message_type}",
        ),
        on_applied=lambda: _dispatch_message(channel, message),
        on_duplicate=lambda: _dispatch_message(channel, message),
    )
    if outcome.action == PROCESS_ACTION_RETRY:
        message_logger.warning("Retrying message due to infra error: %s", outcome.reason)
        return "retry"
    return "ack"


def _forward_to_exchange(
    channel,
    *,
    exchange: str,
    routing_key: str,
    body: bytes,
    properties,
    source_queue: str,
):
    headers = {}
    if properties is not None and isinstance(properties.headers, dict):
        headers = dict(properties.headers)
    headers["x-dds-source-queue"] = source_queue
    headers["x-dds-forwarded-at"] = int(time.time())

    publish_properties = pika.BasicProperties(
        app_id=getattr(properties, "app_id", None),
        content_encoding=getattr(properties, "content_encoding", None),
        content_type=getattr(properties, "content_type", None),
        correlation_id=getattr(properties, "correlation_id", None),
        delivery_mode=2,
        headers=headers,
        message_id=getattr(properties, "message_id", None),
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


def on_message(
    channel,
    method,
    properties,
    body,
    *,
    queue_name: str,
    max_retries: int,
    dlx_exchange: str,
    dlx_routing_key: str,
):
    action = process_delivery(channel, body)
    if action == "ack":
        channel.basic_ack(delivery_tag=method.delivery_tag)
    elif action == "retry":
        try:
            destination = decide_retry_destination(
                getattr(properties, "headers", None),
                queue_name=queue_name,
                max_retries=max_retries,
            )
            if destination == RETRY_DESTINATION_RETRY:
                channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                return
            _forward_to_exchange(
                channel,
                exchange=dlx_exchange,
                routing_key=dlx_routing_key,
                body=body,
                properties=properties,
                source_queue=queue_name,
            )
            channel.basic_ack(delivery_tag=method.delivery_tag)
        except Exception:
            LOGGER.exception("Order worker failed to route retry path; requeueing message")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    else:
        try:
            _forward_to_exchange(
                channel,
                exchange=dlx_exchange,
                routing_key=dlx_routing_key,
                body=body,
                properties=properties,
                source_queue=queue_name,
            )
            channel.basic_ack(delivery_tag=method.delivery_tag)
        except Exception:
            LOGGER.exception("Order worker failed to route rejected message to DLQ; requeueing")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def run_consumer():
    queue_names = _get_queue_names()
    prefetch_count = _get_int_env("WORKER_PREFETCH_COUNT", 1, min_value=1)
    max_retries = _get_int_env("WORKER_MAX_RETRIES", 5, min_value=0)
    reconnect_backoff_ms = _get_int_env("WORKER_RECONNECT_BACKOFF_MS", 2000, min_value=100)
    dlx_exchange = os.environ.get("WORKER_DLX_EXCHANGE", "order.dlx")
    dlx_routing_key = os.environ.get("WORKER_DLX_ROUTING_KEY", "order.dlq")
    startup_recovery_enabled = os.environ.get("WORKER_STARTUP_RECOVERY_ENABLED", "true").lower() not in (
        "0",
        "false",
        "no",
    )
    recovery_stale_after_ms = _get_int_env("RECOVERY_STALE_AFTER_MS", 15000, min_value=1000)
    recovery_batch_size = _get_int_env("RECOVERY_BATCH_SIZE", 200, min_value=1)
    recovery_step_lock_ttl_sec = _get_int_env("RECOVERY_STEP_LOCK_TTL_SEC", 120, min_value=1)
    recovery_startup_max_actions = _get_int_env("RECOVERY_STARTUP_MAX_ACTIONS", 200, min_value=1)
    recovery_leader_lock_ttl_ms = _get_int_env("RECOVERY_LEADER_LOCK_TTL_MS", 10000, min_value=1000)

    while True:
        connection = None
        channel = None
        try:
            connection = pika.BlockingConnection(build_rabbitmq_parameters())
            channel = connection.channel()
            try:
                ensure_rabbitmq_topology(channel)
            except AttributeError:
                LOGGER.debug("Skipping topology bootstrap for mocked channel")
            if startup_recovery_enabled:
                try:
                    stats = run_startup_recovery_once(
                        channel,
                        db,
                        stale_after_ms=recovery_stale_after_ms,
                        batch_size=recovery_batch_size,
                        step_lock_ttl_sec=recovery_step_lock_ttl_sec,
                        max_actions=recovery_startup_max_actions,
                        leader_lock_ttl_ms=recovery_leader_lock_ttl_ms,
                        owner_token=f"order-worker-startup-{uuid.uuid4()}",
                    )
                    if stats is None:
                        LOGGER.info("Startup recovery skipped (leader lock held elsewhere)")
                    else:
                        LOGGER.info(
                            "Startup recovery stats scanned=%s stale=%s reconciled=%s locked_out=%s errors=%s",
                            stats["scanned"],
                            stats["stale"],
                            stats["reconciled"],
                            stats["locked_out"],
                            stats["errors"],
                        )
                except Exception:
                    LOGGER.exception("Startup recovery run failed; continuing with normal consumption")
            channel.basic_qos(prefetch_count=prefetch_count)
            for queue_name in queue_names:
                channel.basic_consume(
                    queue=queue_name,
                    on_message_callback=lambda ch, method, properties, body, queue_name=queue_name: on_message(
                        ch,
                        method,
                        properties,
                        body,
                        queue_name=queue_name,
                        max_retries=max_retries,
                        dlx_exchange=dlx_exchange,
                        dlx_routing_key=dlx_routing_key,
                    ),
                    auto_ack=False,
                )

            LOGGER.info(
                "Order worker consuming queues=%s prefetch=%s max_retries=%s",
                ",".join(queue_names),
                prefetch_count,
                max_retries,
            )
            channel.start_consuming()
        except KeyboardInterrupt:
            LOGGER.info("Order worker shutdown requested")
            break
        except Exception:
            LOGGER.exception(
                "Order worker consumer loop failed; reconnecting in %sms",
                reconnect_backoff_ms,
            )
            time.sleep(reconnect_backoff_ms / 1000)
        finally:
            if channel is not None and channel.is_open:
                channel.close()
            if connection is not None and connection.is_open:
                connection.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_consumer()
