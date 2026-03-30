import os
from collections.abc import Callable


def rabbitmq_url_from_env() -> str:
    return os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")


def run_post_fork_bootstrap(worker, logger, steps: list[tuple[str, Callable[[], None]]]) -> None:
    for label, fn in steps:
        fn()
        logger.info("%s started in pid=%s", label, worker.pid)
