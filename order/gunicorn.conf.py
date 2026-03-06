"""Gunicorn configuration for order-service.

post_fork starts the coordinator's RabbitMQ reply consumer and recovery worker
after each worker process is forked. This ensures each background thread has
fresh process-local connections (not inherited from the parent).
"""
import os
import logging

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    rabbitmq_url = os.environ.get('RABBITMQ_URL', 'amqp://guest:guest@rabbitmq:5672/')

    from coordinator.messaging import init_reply_consumer
    from coordinator.recovery import start_recovery_worker
    from app import _get_coordinator, _TxStoreImpl

    init_reply_consumer(rabbitmq_url)
    start_recovery_worker(
        coordinator=_get_coordinator(),
        tx_store=_TxStoreImpl(),
    )
    logger.info("Order coordinator reply consumer started in pid=%s", worker.pid)
    logger.info("Order recovery worker started in pid=%s", worker.pid)
