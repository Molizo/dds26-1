import logging
import os
import sys
from pathlib import Path

import pika

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.append(str(SERVICE_ROOT))

from messaging import build_rabbitmq_parameters, decide_message_action, get_message_logger

LOGGER = logging.getLogger("order-worker")


def process_delivery(body: bytes) -> str:
    decision = decide_message_action(body)
    if decision.action == "ack" and decision.message is not None:
        message_logger = get_message_logger(LOGGER, decision.message.metadata)
        message_logger.info("Validated message type=%s", decision.message.message_type)
        return "ack"

    LOGGER.warning("Rejected message: %s", decision.reason)
    return "reject"


def on_message(channel, method, properties, body):
    del properties
    action = process_delivery(body)
    if action == "ack":
        channel.basic_ack(delivery_tag=method.delivery_tag)
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
