"""RabbitMQ consumer for internal order-service commands."""
import logging

import redis

from common.constants import ORDER_CMD_MARK_PAID, ORDER_CMD_READ_ORDER, ORDER_COMMANDS_QUEUE
from common.messaging import publish_message, start_participant_consumer
from common.models import (
    InternalCommand,
    InternalReply,
    OrderSnapshotPayload,
    decode_internal_command,
    encode_internal_reply,
)
from store import mark_order_paid, read_order_snapshot

logger = logging.getLogger(__name__)

_db: redis.Redis | None = None
_rabbitmq_url: str | None = None


def init_worker(db: redis.Redis, rabbitmq_url: str) -> None:
    global _db, _rabbitmq_url
    _db = db
    _rabbitmq_url = rabbitmq_url


def start_consumer_thread() -> None:
    if _db is None or _rabbitmq_url is None:
        raise RuntimeError("init_worker() must be called before start_consumer_thread()")
    start_participant_consumer(_rabbitmq_url, ORDER_COMMANDS_QUEUE, _handle_command)


def _handle_command(channel, method, properties, body: bytes) -> None:
    try:
        cmd: InternalCommand = decode_internal_command(body)
    except Exception as exc:
        logger.error("Failed to decode order command: %s", exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    reply_to = properties.reply_to if properties else None
    correlation_id = properties.correlation_id if properties else None
    if not reply_to or not correlation_id:
        logger.error("Dropping order command without reply metadata request=%s", cmd.request_id)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    reply = _dispatch(cmd)
    channel.basic_ack(delivery_tag=method.delivery_tag)

    try:
        publish_message(
            _rabbitmq_url,
            reply_to,
            encode_internal_reply(reply),
            correlation_id=correlation_id,
        )
    except Exception as exc:
        logger.error("Failed to publish order reply request=%s: %s", cmd.request_id, exc)


def _dispatch(cmd: InternalCommand) -> InternalReply:
    if cmd.command == ORDER_CMD_READ_ORDER:
        snapshot = read_order_snapshot(_db, cmd.order_id)
        if snapshot is None:
            return InternalReply(request_id=cmd.request_id, command=cmd.command, ok=False, error="not_found")
        return InternalReply(
            request_id=cmd.request_id,
            command=cmd.command,
            ok=True,
            snapshot=OrderSnapshotPayload(
                order_id=snapshot.order_id,
                user_id=snapshot.user_id,
                total_cost=snapshot.total_cost,
                paid=snapshot.paid,
                items=list(snapshot.items),
            ),
        )

    if cmd.command == ORDER_CMD_MARK_PAID:
        if not cmd.tx_id:
            return InternalReply(request_id=cmd.request_id, command=cmd.command, ok=False, error="tx_id_required")
        marked = mark_order_paid(_db, cmd.order_id)
        if not marked:
            return InternalReply(request_id=cmd.request_id, command=cmd.command, ok=False, error="not_found")
        logger.info("Order marked paid order=%s tx=%s", cmd.order_id, cmd.tx_id)
        return InternalReply(request_id=cmd.request_id, command=cmd.command, ok=True)

    logger.warning("Unknown order command=%s request=%s", cmd.command, cmd.request_id)
    return InternalReply(request_id=cmd.request_id, command=cmd.command, ok=False, error="unknown_command")
