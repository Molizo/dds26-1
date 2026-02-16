import logging
import os
import sys
from pathlib import Path

import pika
import redis

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.append(str(SERVICE_ROOT))

from messaging import build_rabbitmq_parameters, decide_message_action, get_message_logger
from shared_messaging.contracts import ReleaseStockPayload, ReserveStockPayload
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


def process_delivery(body: bytes) -> str:
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
                message_logger.info(
                    "ReserveStock business rejection for order=%s item=%s reason=%s",
                    message.metadata.order_id,
                    item.item_id,
                    outcome.reason,
                )
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
        return "ack"

    message_logger.warning("Unsupported stock worker message type=%s", message.message_type)
    return "reject"


def on_message(channel, method, properties, body):
    del properties
    action = process_delivery(body)
    if action == "ack":
        channel.basic_ack(delivery_tag=method.delivery_tag)
    elif action == "retry":
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    else:
        channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)


def run_consumer():
    queue_name = os.environ.get("RABBITMQ_QUEUE", "stock.command.q")
    connection = pika.BlockingConnection(build_rabbitmq_parameters())
    channel = connection.channel()
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=queue_name, on_message_callback=on_message, auto_ack=False)
    LOGGER.info("Stock worker consuming queue=%s", queue_name)

    try:
        channel.start_consuming()
    finally:
        if channel.is_open:
            channel.close()
        if connection.is_open:
            connection.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_consumer()
