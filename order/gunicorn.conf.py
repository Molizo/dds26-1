"""Gunicorn configuration for order-service."""
import logging
import os

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

    from app import db
    from common.rpc import init_rpc_reply_consumer
    from worker import init_worker, start_consumer_thread

    init_rpc_reply_consumer(rabbitmq_url, prefix="order.rpc.replies")
    init_worker(db, rabbitmq_url)
    start_consumer_thread()
    logger.info("Order RPC reply consumer started in pid=%s", worker.pid)
    logger.info("Order command consumer started in pid=%s", worker.pid)
