"""RabbitMQ connection management and messaging utilities.

Thread-safety model:
- Publisher connections: thread-local — each Flask worker thread gets its own
  pika BlockingConnection + channel. This avoids pika's not-thread-safe channel
  sharing, which was a source of bugs in attempt 1.
- Participant consumer: one dedicated background thread per process with its
  own connection, consuming from stock.commands or payment.commands.
- Reply consumer (coordinator): one dedicated background thread per process
  consuming from an exclusive auto-delete reply queue. Added in Step 2.
- Correlation map: a shared {tx_id → (Event, result_slot)} dict protected by
  a threading.Lock. Request threads register before publishing; consumer thread
  signals. Added in Step 2.

Gunicorn prefork constraint:
  All RabbitMQ connections must be created AFTER gunicorn forks. Connections
  opened before fork leave stale file descriptors in child processes that fail
  silently or raise exceptions. Use gunicorn's post_fork hook to start threads.
"""
import logging
import threading
import time
from typing import Callable, Optional

import pika

from common.constants import (
    STOCK_COMMANDS_QUEUE,
    PAYMENT_COMMANDS_QUEUE,
    STOCK_COMMANDS_DLQ,
    PAYMENT_COMMANDS_DLQ,
    ORDER_COMMANDS_QUEUE,
    ORCHESTRATOR_COMMANDS_QUEUE,
    ORDER_COMMANDS_DLQ,
    ORCHESTRATOR_COMMANDS_DLQ,
)
from common.env import get_positive_int_env

logger = logging.getLogger(__name__)

# Thread-local storage: each request thread owns its own pika connection.
_thread_local = threading.local()
# ---------------------------------------------------------------------------
# Queue topology
# ---------------------------------------------------------------------------

def _declare_queues(channel) -> None:
    """Declare all durable command queues and their DLQs.

    Idempotent — safe to call on every reconnect. Dead-letter queues are
    declared first so the main queues can reference them immediately.
    """
    channel.queue_declare(queue=STOCK_COMMANDS_DLQ, durable=True)
    channel.queue_declare(queue=PAYMENT_COMMANDS_DLQ, durable=True)
    channel.queue_declare(queue=ORDER_COMMANDS_DLQ, durable=True)
    channel.queue_declare(queue=ORCHESTRATOR_COMMANDS_DLQ, durable=True)
    channel.queue_declare(
        queue=STOCK_COMMANDS_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": STOCK_COMMANDS_DLQ,
        },
    )
    channel.queue_declare(
        queue=PAYMENT_COMMANDS_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": PAYMENT_COMMANDS_DLQ,
        },
    )
    channel.queue_declare(
        queue=ORDER_COMMANDS_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": ORDER_COMMANDS_DLQ,
        },
    )
    channel.queue_declare(
        queue=ORCHESTRATOR_COMMANDS_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": ORCHESTRATOR_COMMANDS_DLQ,
        },
    )


# ---------------------------------------------------------------------------
# Publisher (thread-local)
# ---------------------------------------------------------------------------

def _get_publisher_channel(rabbitmq_url: str):
    """Return a live thread-local channel, reconnecting if needed."""
    local = _thread_local
    needs_reconnect = (
        not hasattr(local, "connection")
        or local.connection is None
        or local.connection.is_closed
        or not hasattr(local, "channel")
        or local.channel is None
        or local.channel.is_closed
    )
    if needs_reconnect:
        try:
            if hasattr(local, "connection") and local.connection and not local.connection.is_closed:
                local.connection.close()
        except Exception:
            pass
        local.connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
        local.channel = local.connection.channel()
        _declare_queues(local.channel)
    return local.channel


def publish_command(rabbitmq_url: str, queue: str, body: bytes, reply_to: str) -> None:
    """Publish a transaction command to a participant queue.

    Uses PERSISTENT delivery so messages survive a broker restart.
    Retries up to 3 times on connection errors; raises on final failure.
    """
    publish_message(rabbitmq_url, queue, body, reply_to=reply_to)


def publish_reply(rabbitmq_url: str, reply_to: str, body: bytes) -> None:
    """Publish a participant reply to the coordinator's reply queue."""
    publish_message(rabbitmq_url, reply_to, body)


def publish_message(
    rabbitmq_url: str,
    routing_key: str,
    body: bytes,
    *,
    reply_to: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> None:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            channel = _get_publisher_channel(rabbitmq_url)
            props = pika.BasicProperties(
                delivery_mode=2,  # persistent
                reply_to=reply_to,
                correlation_id=correlation_id,
            )
            channel.basic_publish(
                exchange="",
                routing_key=routing_key,
                body=body,
                properties=props,
            )
            return
        except Exception as exc:
            logger.warning("Publish attempt %d/%d failed: %s", attempt + 1, max_retries, exc)
            # Drop the broken connection so the next attempt reconnects.
            _thread_local.channel = None
            _thread_local.connection = None
            if attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
            else:
                raise


# ---------------------------------------------------------------------------
# Participant consumer (background thread)
# ---------------------------------------------------------------------------

def start_participant_consumer(
    rabbitmq_url: str,
    queue: str,
    handler: Callable,
) -> threading.Thread:
    """Start a daemon thread that consumes commands from a participant queue.

    The thread reconnects automatically on failure, so transient broker
    restarts do not require a service restart.

    handler signature: (channel, method, properties, body) -> None
      The handler must ack or nack the message before returning.
    """
    thread = threading.Thread(
        target=_run_participant_consumer,
        args=(rabbitmq_url, queue, handler),
        daemon=True,
        name=f"consumer-{queue}",
    )
    thread.start()
    logger.info("Participant consumer thread started for queue=%s", queue)
    return thread


def _run_participant_consumer(rabbitmq_url: str, queue: str, handler: Callable) -> None:
    """Consumer thread body. Reconnects on any error with exponential backoff."""
    backoff = 1
    prefetch_count = get_positive_int_env("PARTICIPANT_CONSUMER_PREFETCH_COUNT", 1, logger)
    while True:
        try:
            conn = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
            channel = conn.channel()
            _declare_queues(channel)
            channel.basic_qos(prefetch_count=prefetch_count)
            channel.basic_consume(queue=queue, on_message_callback=handler)
            logger.info("Consumer ready on queue=%s", queue)
            backoff = 1  # reset on successful connect
            channel.start_consuming()
        except Exception as exc:
            logger.error("Consumer error on queue=%s: %s — reconnecting in %ds", queue, exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
