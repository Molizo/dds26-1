"""RabbitMQ consumer for the stock service.

Consumes ParticipantCommand messages from stock.commands, dispatches to
atomic Lua-based service operations, and publishes a ParticipantReply to
the coordinator's reply queue specified in each command's reply_to field.

The consumer thread is started via gunicorn's post_fork hook so each
worker process has exactly one consumer thread with its own connection.
"""
import logging
import os

import redis

from common.constants import CMD_HOLD, CMD_RELEASE, CMD_COMMIT, SVC_STOCK
from common.messaging import publish_reply, start_participant_consumer
from common.models import (
    ParticipantCommand,
    ParticipantReply,
    decode_command,
    encode_reply,
)
from common.constants import STOCK_COMMANDS_QUEUE
import service as stock_service

logger = logging.getLogger(__name__)

_db: redis.Redis | None = None
_rabbitmq_url: str | None = None


def init_worker(db: redis.Redis, rabbitmq_url: str) -> None:
    """Called from gunicorn post_fork hook to inject dependencies."""
    global _db, _rabbitmq_url
    _db = db
    _rabbitmq_url = rabbitmq_url


def start_consumer_thread() -> None:
    """Start the RabbitMQ consumer daemon thread. Called after gunicorn fork."""
    if _db is None or _rabbitmq_url is None:
        raise RuntimeError("init_worker() must be called before start_consumer_thread()")
    start_participant_consumer(_rabbitmq_url, STOCK_COMMANDS_QUEUE, _handle_command)


def _handle_command(channel, method, properties, body: bytes) -> None:
    """Process one ParticipantCommand message.

    Always acks the message before returning so it is not requeued.
    Errors in processing are reported via ParticipantReply(ok=False) rather
    than nacking, which would cause infinite redelivery for logic errors.
    """
    try:
        cmd: ParticipantCommand = decode_command(body)
    except Exception as exc:
        logger.error("Failed to decode stock command: %s", exc)
        # Nack without requeue — malformed messages go to DLQ
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    logger.info("stock cmd tx=%s command=%s", cmd.tx_id, cmd.command)
    reply = _dispatch(cmd)
    channel.basic_ack(delivery_tag=method.delivery_tag)

    try:
        publish_reply(_rabbitmq_url, cmd.reply_to, encode_reply(reply))
    except Exception as exc:
        # Reply publish failed; the coordinator will time out and mark
        # FAILED_NEEDS_RECOVERY. Recovery will re-publish the command, and
        # the Lua script's idempotency check will return the cached result.
        logger.error("Failed to publish reply for tx=%s: %s", cmd.tx_id, exc)


def _dispatch(cmd: ParticipantCommand) -> ParticipantReply:
    """Route command to the correct service operation and wrap the result."""
    tx_id = cmd.tx_id
    command = cmd.command

    try:
        if command == CMD_HOLD:
            if cmd.stock_payload is None:
                return _reply(cmd, ok=False, error="missing_payload")
            result = stock_service.hold_stock(_db, tx_id, cmd.stock_payload.items)
            return _reply(cmd, ok=result["ok"], error=result.get("error"))

        elif command == CMD_RELEASE:
            if cmd.stock_payload is None:
                return _reply(cmd, ok=False, error="missing_payload")
            result = stock_service.release_stock(_db, tx_id, cmd.stock_payload.items)
            # tx_not_found is a harmless no-op — coordinator treats it as success
            ok = result["ok"] or result.get("error") == "tx_not_found"
            return _reply(cmd, ok=ok, error=None if ok else result.get("error"))

        elif command == CMD_COMMIT:
            result = stock_service.commit_stock(_db, tx_id)
            ok = result["ok"] or result.get("error") == "tx_not_found"
            return _reply(cmd, ok=ok, error=None if ok else result.get("error"))

        else:
            logger.warning("Unknown stock command: %s tx=%s", command, tx_id)
            return _reply(cmd, ok=False, error="unknown_command")

    except Exception as exc:
        logger.error("Unhandled error in stock dispatch tx=%s: %s", tx_id, exc)
        return _reply(cmd, ok=False, error="internal_error")


def _reply(cmd: ParticipantCommand, ok: bool, error: str | None) -> ParticipantReply:
    return ParticipantReply(
        tx_id=cmd.tx_id,
        service=SVC_STOCK,
        command=cmd.command,
        ok=ok,
        error=error,
    )
