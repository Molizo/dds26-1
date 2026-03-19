"""Gunicorn configuration for order-service."""
import logging

from common.gunicorn import rabbitmq_url_from_env, run_post_fork_bootstrap

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    from app import db
    from common.rpc import init_rpc_reply_consumer
    from worker import init_worker, start_consumer_thread

    rabbitmq_url = rabbitmq_url_from_env()
    run_post_fork_bootstrap(
        worker,
        logger,
        [
            ("Order RPC reply consumer", lambda: init_rpc_reply_consumer(rabbitmq_url, prefix="order.rpc.replies")),
            ("Order command consumer", lambda: (init_worker(db, rabbitmq_url), start_consumer_thread())),
        ],
    )
