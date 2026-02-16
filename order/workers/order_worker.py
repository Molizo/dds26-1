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
from shared_messaging.idempotency import PROCESS_ACTION_RETRY, process_idempotent_step
from shared_messaging.redis_atomic import ATOMIC_APPLIED, ATOMIC_DUPLICATE, AtomicResult
from shared_messaging.redis_keys import EFFECT_TTL_SEC, INBOX_TTL_SEC, effect_key, inbox_key

LOGGER = logging.getLogger("order-worker")
db: redis.Redis = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    password=os.environ["REDIS_PASSWORD"],
    db=int(os.environ["REDIS_DB"]),
    decode_responses=True,
)


def mark_order_message_processed(message_id: str, order_id: str, step: str) -> AtomicResult:
    marker_key = inbox_key("order-worker", message_id)
    applied = db.set(marker_key, "1", ex=INBOX_TTL_SEC, nx=True)
    if not applied:
        return AtomicResult(status=ATOMIC_DUPLICATE, raw_code=0)
    db.set(effect_key("order-worker", order_id, step), "1", ex=EFFECT_TTL_SEC)
    return AtomicResult(status=ATOMIC_APPLIED, raw_code=1)


def process_delivery(body: bytes) -> str:
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
        )
    )
    if outcome.action == PROCESS_ACTION_RETRY:
        message_logger.warning("Retrying message due to infra error: %s", outcome.reason)
        return "retry"
    return "ack"


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
    queue_name = os.environ.get("RABBITMQ_QUEUE", "order.command.q")
    connection = pika.BlockingConnection(build_rabbitmq_parameters())
    channel = connection.channel()
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=queue_name, on_message_callback=on_message, auto_ack=False)
    LOGGER.info("Order worker consuming queue=%s", queue_name)

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
