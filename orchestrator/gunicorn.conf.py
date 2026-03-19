"""Gunicorn configuration for orchestrator-service.

Each worker starts its own reply-consumer thread. Recovery startup also runs in
each worker, but only the current Redis leader performs scans.
"""
import logging
import os

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")

    from common.rpc import init_rpc_reply_consumer
    from coordinator.messaging import init_reply_consumer
    from coordinator.recovery import start_recovery_worker
    from app import _get_coordinator, _TxStoreImpl
    from worker import init_worker, start_consumer_thread

    init_rpc_reply_consumer(rabbitmq_url, prefix="orchestrator.rpc.replies")
    init_reply_consumer(rabbitmq_url)
    init_worker(rabbitmq_url)
    start_consumer_thread()
    start_recovery_worker(
        coordinator=_get_coordinator(),
        tx_store=_TxStoreImpl(),
    )
    logger.info("Orchestrator RPC reply consumer started in pid=%s", worker.pid)
    logger.info("Orchestrator reply consumer started in pid=%s", worker.pid)
    logger.info("Orchestrator command consumer started in pid=%s", worker.pid)
    logger.info("Orchestrator recovery worker started in pid=%s", worker.pid)
