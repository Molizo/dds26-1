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
from common.messaging import start_participant_consumer
from common.models import (
    ParticipantCommand,
    ParticipantReply,
    decode_command,
    encode_reply,
)
from common.worker_support import handle_participant_delivery
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
    """Process one ParticipantCommand message."""
    handle_participant_delivery(
        channel=channel,
        method=method,
        body=body,
        rabbitmq_url=_rabbitmq_url,
        service_name="stock",
        decode_command=decode_command,
        dispatch=_dispatch,
        encode_reply=encode_reply,
        logger=logger,
    )


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
