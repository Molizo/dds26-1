"""Gunicorn configuration for stock-service.

post_fork starts the RabbitMQ consumer thread after each worker process is
forked. This ensures the consumer has its own connection (not inherited from
the parent) and avoids stale file descriptor issues.
"""
import os
import logging

from common.gunicorn import rabbitmq_url_from_env, run_post_fork_bootstrap

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    # Import db from the Flask app (already initialised at module scope)
    from app import db
    from worker import init_worker, start_consumer_thread

    rabbitmq_url = rabbitmq_url_from_env()
    run_post_fork_bootstrap(
        worker,
        logger,
        [
            ("Stock worker consumer", lambda: (init_worker(db, rabbitmq_url), start_consumer_thread())),
        ],
    )
