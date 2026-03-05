"""Gunicorn configuration for stock-service.

post_fork starts the RabbitMQ consumer thread after each worker process is
forked. This ensures the consumer has its own connection (not inherited from
the parent) and avoids stale file descriptor issues.
"""
import os
import logging

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    rabbitmq_url = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@rabbitmq:5672/')

    # Import db from the Flask app (already initialised at module scope)
    from app import db
    from worker import init_worker, start_consumer_thread

    init_worker(db, rabbitmq_url)
    start_consumer_thread()
    logger.info("Stock worker consumer started in pid=%s", worker.pid)
