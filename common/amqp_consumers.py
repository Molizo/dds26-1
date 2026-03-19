from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable

import pika


def start_exclusive_reply_consumer(
    rabbitmq_url: str,
    *,
    queue_prefix: str,
    thread_name_prefix: str,
    prefetch_count: int,
    on_message_callback: Callable,
    logger,
) -> tuple[str, threading.Thread]:
    queue_name = f"{queue_prefix}.{uuid.uuid4().hex[:12]}"
    thread = threading.Thread(
        target=_run_exclusive_reply_consumer,
        args=(rabbitmq_url, queue_name, prefetch_count, on_message_callback, logger),
        daemon=True,
        name=f"{thread_name_prefix}-{queue_name}",
    )
    thread.start()
    logger.info("%s thread started for queue=%s", thread_name_prefix, queue_name)
    return queue_name, thread


def _run_exclusive_reply_consumer(
    rabbitmq_url: str,
    queue_name: str,
    prefetch_count: int,
    on_message_callback: Callable,
    logger,
) -> None:
    backoff = 1
    while True:
        try:
            conn = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
            channel = conn.channel()
            channel.queue_declare(queue=queue_name, exclusive=True, auto_delete=True)
            channel.basic_qos(prefetch_count=prefetch_count)
            channel.basic_consume(queue=queue_name, on_message_callback=on_message_callback)
            logger.info("Exclusive reply consumer ready on queue=%s", queue_name)
            backoff = 1
            channel.start_consuming()
        except Exception as exc:
            logger.error(
                "Exclusive reply consumer error on queue=%s: %s; reconnecting in %ds",
                queue_name,
                exc,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
