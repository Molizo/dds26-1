"""Gunicorn configuration for payment-service."""
import os
import logging

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    rabbitmq_url = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@rabbitmq:5672/')

    from app import db
    from worker import init_worker, start_consumer_thread

    init_worker(db, rabbitmq_url)
    start_consumer_thread()
    logger.info("Payment worker consumer started in pid=%s", worker.pid)
