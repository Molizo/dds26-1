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

from messaging import (
    build_rabbitmq_parameters,
    decide_message_action,
    encode_internal_message,
    ensure_rabbitmq_topology,
    get_message_logger,
)
from shared_messaging.consumer import RETRY_DESTINATION_RETRY, decide_retry_destination
from shared_messaging.contracts import (
    MessageMetadata,
    ReleaseStockPayload,
    ReserveStockPayload,
    StockRejectedPayload,
    StockReleasedPayload,
    StockReservedPayload,
)
from shared_messaging.idempotency import PROCESS_ACTION_RETRY, process_idempotent_step
from shared_messaging.redis_atomic import release_stock_atomic, reserve_stock_atomic

LOGGER = logging.getLogger("stock-worker")
db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
    decode_responses=True,
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


def _build_next_metadata(source: MessageMetadata, *, step: str) -> MessageMetadata:
    return MessageMetadata(
        message_id=str(uuid.uuid4()),
        saga_id=source.saga_id,
        order_id=source.order_id,
        step=step,
        attempt=0,
        timestamp=int(time.time() * 1000),
        correlation_id=source.correlation_id,
        causation_id=source.message_id,
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
        ),
        mandatory=False,
    )


def _publish_stock_reserved(channel, metadata: MessageMetadata, payload: ReserveStockPayload) -> None:
    next_metadata = _build_next_metadata(metadata, step="stock_reserved")
    _publish_internal_message(
        channel,
        exchange="order.command",
        routing_key="order.command",
        message_type="StockReserved",
        metadata=next_metadata,
        payload=StockReservedPayload(items=payload.items),
    )


def _publish_stock_rejected(channel, metadata: MessageMetadata, reason: str) -> None:
    next_metadata = _build_next_metadata(metadata, step="stock_rejected")
    _publish_internal_message(
        channel,
        exchange="order.command",
        routing_key="order.command",
        message_type="StockRejected",
        metadata=next_metadata,
        payload=StockRejectedPayload(reason=reason),
    )


def _publish_stock_released(channel, metadata: MessageMetadata, payload: ReleaseStockPayload) -> None:
    next_metadata = _build_next_metadata(metadata, step="stock_released")
    _publish_internal_message(
        channel,
        exchange="order.command",
        routing_key="order.command",
        message_type="StockReleased",
        metadata=next_metadata,
        payload=StockReleasedPayload(items=payload.items),
    )


def _rollback_reserved_items(metadata, reserved_items) -> str | None:
    for item in reserved_items:
        rollback_outcome = process_idempotent_step(
            apply_effect=lambda item=item: release_stock_atomic(
                db,
                service="stock-worker",
                message_id=f"{metadata.message_id}:{item.item_id}:rollback",
                item_id=item.item_id,
                step=f"{metadata.step}:rollback",
                quantity=item.quantity,
            )
        )
        if rollback_outcome.action == PROCESS_ACTION_RETRY:
            return rollback_outcome.reason
    return None


def process_delivery(channel, body: bytes) -> str:
    decision = decide_message_action(body)
    if decision.action != "ack" or decision.message is None:
        LOGGER.warning("Rejected message: %s", decision.reason)
        return "reject"

    message = decision.message
    message_logger = get_message_logger(LOGGER, message.metadata)
    message_logger.info("Validated message type=%s", message.message_type)

    if message.message_type == "ReserveStock":
        payload = message.payload
        if not isinstance(payload, ReserveStockPayload):
            message_logger.warning("Invalid payload type for ReserveStock")
            return "reject"

        reserved_items = []
        for item in payload.items:
            outcome = process_idempotent_step(
                apply_effect=lambda item=item: reserve_stock_atomic(
                    db,
                    service="stock-worker",
                    message_id=f"{message.metadata.message_id}:{item.item_id}",
                    item_id=item.item_id,
                    step=f"{message.metadata.step}:reserve",
                    quantity=item.quantity,
                )
            )
            if outcome.action == PROCESS_ACTION_RETRY:
                message_logger.warning("Retrying ReserveStock due to infra error: %s", outcome.reason)
                return "retry"
            if outcome.status == "business_reject":
                rollback_error = _rollback_reserved_items(message.metadata, reserved_items)
                if rollback_error is not None:
                    message_logger.warning("Retrying ReserveStock rollback due to infra error: %s", rollback_error)
                    return "retry"
                try:
                    _publish_stock_rejected(channel, message.metadata, "stock_rejected")
                except Exception:
                    message_logger.warning("Retrying ReserveStock due to stock rejection publish failure")
                    return "retry"
                return "ack"
            reserved_items.append(item)

        try:
            _publish_stock_reserved(channel, message.metadata, payload)
        except Exception:
            message_logger.warning("Retrying ReserveStock due to StockReserved publish failure")
            return "retry"
        return "ack"

    if message.message_type == "ReleaseStock":
        payload = message.payload
        if not isinstance(payload, ReleaseStockPayload):
            message_logger.warning("Invalid payload type for ReleaseStock")
            return "reject"
        for item in payload.items:
            outcome = process_idempotent_step(
                apply_effect=lambda item=item: release_stock_atomic(
                    db,
                    service="stock-worker",
                    message_id=f"{message.metadata.message_id}:{item.item_id}",
                    item_id=item.item_id,
                    step=f"{message.metadata.step}:release",
                    quantity=item.quantity,
                )
            )
            if outcome.action == PROCESS_ACTION_RETRY:
                message_logger.warning("Retrying ReleaseStock due to infra error: %s", outcome.reason)
                return "retry"

        try:
            _publish_stock_released(channel, message.metadata, payload)
        except Exception:
            message_logger.warning("Retrying ReleaseStock due to StockReleased publish failure")
            return "retry"
        return "ack"

    message_logger.warning("Unsupported stock worker message type=%s", message.message_type)
    return "reject"


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
            LOGGER.exception("Stock worker failed to route retry path; requeueing message")
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
            LOGGER.exception("Stock worker failed to route rejected message to DLQ; requeueing")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def run_consumer():
    queue_name = os.environ.get("RABBITMQ_QUEUE", "stock.command.q")
    prefetch_count = _get_int_env("WORKER_PREFETCH_COUNT", 1, min_value=1)
    max_retries = _get_int_env("WORKER_MAX_RETRIES", 5, min_value=0)
    reconnect_backoff_ms = _get_int_env("WORKER_RECONNECT_BACKOFF_MS", 2000, min_value=100)
    dlx_exchange = os.environ.get("WORKER_DLX_EXCHANGE", "stock.dlx")
    dlx_routing_key = os.environ.get("WORKER_DLX_ROUTING_KEY", "stock.dlq")

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
            channel.basic_qos(prefetch_count=prefetch_count)
            channel.basic_consume(
                queue=queue_name,
                on_message_callback=lambda ch, method, properties, body: on_message(
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
                "Stock worker consuming queue=%s prefetch=%s max_retries=%s",
                queue_name,
                prefetch_count,
                max_retries,
            )
            channel.start_consuming()
        except KeyboardInterrupt:
            LOGGER.info("Stock worker shutdown requested")
            break
        except Exception:
            LOGGER.exception(
                "Stock worker consumer loop failed; reconnecting in %sms",
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
