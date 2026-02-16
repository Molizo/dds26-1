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
from shared_messaging.contracts import ChargePaymentPayload
from shared_messaging.idempotency import PROCESS_ACTION_RETRY, process_idempotent_step
from shared_messaging.redis_atomic import charge_payment_atomic

LOGGER = logging.getLogger("payment-worker")
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

    if message.message_type != "ChargePayment":
        message_logger.warning("Unsupported payment worker message type=%s", message.message_type)
        return "reject"

    payload = message.payload
    if not isinstance(payload, ChargePaymentPayload):
        message_logger.warning("Invalid payload type for ChargePayment")
        return "reject"
    outcome = process_idempotent_step(
        apply_effect=lambda: charge_payment_atomic(
            db,
            service="payment-worker",
            message_id=message.metadata.message_id,
            user_id=payload.user_id,
            step=f"{message.metadata.step}:charge",
            amount=payload.amount,
        )
    )

    if outcome.action == PROCESS_ACTION_RETRY:
        message_logger.warning("Retrying ChargePayment due to infra error: %s", outcome.reason)
        return "retry"

    if outcome.status == "business_reject":
        message_logger.info(
            "ChargePayment business rejection for order=%s user=%s reason=%s",
            message.metadata.order_id,
            payload.user_id,
            outcome.reason,
        )
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
    queue_name = os.environ.get("RABBITMQ_QUEUE", "payment.command.q")
    connection = pika.BlockingConnection(build_rabbitmq_parameters())
    channel = connection.channel()
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=queue_name, on_message_callback=on_message, auto_ack=False)
    LOGGER.info("Payment worker consuming queue=%s", queue_name)

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
