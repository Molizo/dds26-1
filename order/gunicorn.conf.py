"""Gunicorn configuration for order-service.

post_fork starts the coordinator's RabbitMQ reply consumer after each worker
process is forked. This ensures the consumer has its own connection (not
inherited from the parent) and avoids stale file descriptor issues.
"""
import os
import logging

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    rabbitmq_url = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@rabbitmq:5672/')

    from coordinator.messaging import init_reply_consumer
    init_reply_consumer(rabbitmq_url)
    logger.info("Order coordinator reply consumer started in pid=%s", worker.pid)
