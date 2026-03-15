"""Coordinator-side RabbitMQ messaging: reply queue consumer and correlation.

Each gunicorn worker process creates:
1. An exclusive auto-delete reply queue (coordinator.replies.{uuid})
2. A background consumer thread that reads replies and signals waiting threads

Request threads use register_pending() before publishing commands, then
wait_for_replies() with a bounded timeout. The consumer thread stores results
and signals the Event when all expected replies have arrived.

Thread-safety:
- _correlation_map is protected by _correlation_lock.
- Publisher connections are thread-local (handled by common.messaging).
"""
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import pika

from common.models import ParticipantReply, decode_reply

logger = logging.getLogger(__name__)


def _get_positive_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
        if parsed > 0:
            return parsed
    except ValueError:
        pass
    logger.warning("Invalid %s=%r, using default=%d", name, value, default)
    return default

# ---------------------------------------------------------------------------
# Correlation map
# ---------------------------------------------------------------------------

@dataclass
class _PendingEntry:
    """Slot for collecting participant replies for one tx_id + command phase."""
    expected_command: str  # hold / commit / release for this pending wait
    expected_services: frozenset  # set of service names we are waiting on
    replies: list[ParticipantReply] = field(default_factory=list)
    replied_services: set = field(default_factory=set)  # dedup by service name
    event: threading.Event = field(default_factory=threading.Event)


_correlation_lock = threading.Lock()
_correlation_map: dict[str, _PendingEntry] = {}

# The reply queue name for this process (set in init_reply_consumer)
_reply_queue: Optional[str] = None


def get_reply_queue() -> str:
    """Return the reply queue name for this process. Must be called after init."""
    if _reply_queue is None:
        raise RuntimeError("Reply consumer not initialised — call init_reply_consumer() first")
    return _reply_queue


def register_pending(tx_id: str, expected_command: str, expected_services: frozenset) -> None:
    """Register a tx_id before publishing commands.

    expected_services: frozenset of service names whose replies are required
    (e.g. frozenset({"stock", "payment"})).
    """
    with _correlation_lock:
        _correlation_map[tx_id] = _PendingEntry(
            expected_command=expected_command,
            expected_services=expected_services,
        )


def wait_for_replies(tx_id: str, timeout: float) -> list[ParticipantReply]:
    """Block until all expected replies arrive or timeout expires.

    Returns the list of replies received (may be fewer than expected on timeout).
    Always cleans up the correlation entry.
    """
    with _correlation_lock:
        entry = _correlation_map.get(tx_id)
    if entry is None:
        return []

    entry.event.wait(timeout=timeout)

    with _correlation_lock:
        entry = _correlation_map.pop(tx_id, entry)
    return list(entry.replies)


def cancel_pending(tx_id: str) -> None:
    """Remove a pending entry without waiting (cleanup on error paths)."""
    with _correlation_lock:
        _correlation_map.pop(tx_id, None)


# ---------------------------------------------------------------------------
# Reply consumer (background thread)
# ---------------------------------------------------------------------------

def init_reply_consumer(rabbitmq_url: str) -> threading.Thread:
    """Create the exclusive reply queue and start the consumer thread.

    Must be called AFTER gunicorn fork (in post_fork hook).
    Returns the daemon thread.
    """
    global _reply_queue
    _reply_queue = f"coordinator.replies.{uuid.uuid4().hex[:12]}"

    thread = threading.Thread(
        target=_run_reply_consumer,
        args=(rabbitmq_url, _reply_queue),
        daemon=True,
        name=f"reply-consumer-{_reply_queue}",
    )
    thread.start()
    logger.info("Reply consumer thread started for queue=%s", _reply_queue)
    return thread


def _run_reply_consumer(rabbitmq_url: str, queue_name: str) -> None:
    """Consumer thread body. Auto-reconnects with exponential backoff."""
    backoff = 1
    prefetch_count = _get_positive_int_env("COORDINATOR_REPLY_PREFETCH_COUNT", 10)
    while True:
        try:
            conn = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
            channel = conn.channel()
            # Declare exclusive auto-delete queue
            channel.queue_declare(queue=queue_name, exclusive=True, auto_delete=True)
            channel.basic_qos(prefetch_count=prefetch_count)
            channel.basic_consume(queue=queue_name, on_message_callback=_on_reply)
            logger.info("Reply consumer ready on queue=%s", queue_name)
            backoff = 1
            channel.start_consuming()
        except Exception as exc:
            logger.error("Reply consumer error on queue=%s: %s — reconnecting in %ds",
                         queue_name, exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _on_reply(channel, method, properties, body: bytes) -> None:
    """Handle one ParticipantReply from the reply queue."""
    channel.basic_ack(delivery_tag=method.delivery_tag)

    try:
        reply = decode_reply(body)
    except Exception as exc:
        logger.error("Failed to decode reply: %s", exc)
        return

    with _correlation_lock:
        entry = _correlation_map.get(reply.tx_id)
        if entry is None:
            # Stale reply for a tx_id we're not waiting on (timeout already passed)
            logger.debug("Ignoring stale reply for tx=%s service=%s", reply.tx_id, reply.service)
            return
        if reply.command != entry.expected_command:
            logger.debug(
                "Ignoring out-of-phase reply for tx=%s service=%s command=%s expected=%s",
                reply.tx_id,
                reply.service,
                reply.command,
                entry.expected_command,
            )
            return
        if reply.service not in entry.expected_services:
            logger.debug(
                "Ignoring unexpected-service reply for tx=%s service=%s command=%s",
                reply.tx_id,
                reply.service,
                reply.command,
            )
            return
        if reply.service in entry.replied_services:
            logger.debug(
                "Ignoring duplicate reply for tx=%s service=%s command=%s",
                reply.tx_id,
                reply.service,
                reply.command,
            )
            return
        entry.replied_services.add(reply.service)
        entry.replies.append(reply)
        if entry.replied_services >= entry.expected_services:
            entry.event.set()
