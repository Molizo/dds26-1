"""Internal RabbitMQ RPC support for order-service and orchestrator workers."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import pika

from common.messaging import publish_message

logger = logging.getLogger(__name__)


@dataclass
class _PendingReply:
    body: Optional[bytes] = None
    event: threading.Event = field(default_factory=threading.Event)


_reply_lock = threading.Lock()
_pending_replies: dict[str, _PendingReply] = {}
_reply_queue: Optional[str] = None


def init_rpc_reply_consumer(rabbitmq_url: str, prefix: str = "rpc.replies") -> threading.Thread:
    """Start one reply consumer per process after gunicorn fork."""
    global _reply_queue
    _reply_queue = f"{prefix}.{uuid.uuid4().hex[:12]}"
    thread = threading.Thread(
        target=_run_reply_consumer,
        args=(rabbitmq_url, _reply_queue),
        daemon=True,
        name=f"rpc-reply-consumer-{_reply_queue}",
    )
    thread.start()
    logger.info("RPC reply consumer thread started for queue=%s", _reply_queue)
    return thread


def get_rpc_reply_queue() -> str:
    if _reply_queue is None:
        raise RuntimeError("RPC reply consumer not initialised")
    return _reply_queue


def cancel_pending_rpc(request_id: str) -> None:
    with _reply_lock:
        _pending_replies.pop(request_id, None)


def rpc_request_bytes(
    rabbitmq_url: str,
    queue_name: str,
    *,
    request_id: str,
    body: bytes,
    timeout_seconds: float,
) -> Optional[bytes]:
    """Publish one RPC request and wait for one reply body."""
    reply_queue = get_rpc_reply_queue()
    entry = _PendingReply()
    with _reply_lock:
        _pending_replies[request_id] = entry

    try:
        publish_message(
            rabbitmq_url,
            queue_name,
            body,
            reply_to=reply_queue,
            correlation_id=request_id,
        )
    except Exception:
        cancel_pending_rpc(request_id)
        raise

    entry.event.wait(timeout=timeout_seconds)
    with _reply_lock:
        resolved = _pending_replies.pop(request_id, entry)
    return resolved.body


def _run_reply_consumer(rabbitmq_url: str, queue_name: str) -> None:
    backoff = 1
    while True:
        try:
            conn = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
            channel = conn.channel()
            channel.queue_declare(queue=queue_name, exclusive=True, auto_delete=True)
            channel.basic_qos(prefetch_count=10)
            channel.basic_consume(queue=queue_name, on_message_callback=_on_reply)
            logger.info("RPC reply consumer ready on queue=%s", queue_name)
            backoff = 1
            channel.start_consuming()
        except Exception as exc:
            logger.error(
                "RPC reply consumer error on queue=%s: %s — reconnecting in %ds",
                queue_name,
                exc,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _on_reply(channel, method, properties, body: bytes) -> None:
    channel.basic_ack(delivery_tag=method.delivery_tag)
    request_id = properties.correlation_id if properties else None
    if not request_id:
        logger.warning("Ignoring RPC reply without correlation id")
        return

    with _reply_lock:
        entry = _pending_replies.get(request_id)
        if entry is None:
            logger.debug("Ignoring stale RPC reply for request=%s", request_id)
            return
        entry.body = body
        entry.event.set()
