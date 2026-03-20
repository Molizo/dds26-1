"""Gunicorn configuration for orchestrator-service.

Each worker starts its own reply-consumer thread. Recovery startup also runs in
each worker, but only the current Redis leader performs scans.
"""
import logging

from common.gunicorn import rabbitmq_url_from_env, run_post_fork_bootstrap

logger = logging.getLogger(__name__)


def post_fork(server, worker):
    from common.rpc import init_rpc_reply_consumer
    from orchestrator.messaging import init_reply_consumer
    from orchestrator.recovery import start_recovery_worker
    from orchestrator.app import _get_runtime
    from orchestrator.worker import init_worker, start_consumer_thread

    rabbitmq_url = rabbitmq_url_from_env()
    runtime = _get_runtime()
    run_post_fork_bootstrap(
        worker,
        logger,
        [
            (
                "Orchestrator RPC reply consumer",
                lambda: init_rpc_reply_consumer(rabbitmq_url, prefix="orchestrator.rpc.replies"),
            ),
            ("Orchestrator reply consumer", lambda: init_reply_consumer(rabbitmq_url)),
            (
                "Orchestrator command consumer",
                lambda: (init_worker(runtime, rabbitmq_url), start_consumer_thread()),
            ),
            (
                "Orchestrator recovery worker",
                lambda: start_recovery_worker(
                    coordinator=runtime.coordinator,
                    tx_store=runtime.tx_store,
                ),
            ),
        ],
    )
