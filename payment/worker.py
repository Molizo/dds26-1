"""RabbitMQ consumer for the payment service.

Mirrors the structure of stock/worker.py — see that module for design notes.
"""
import logging

import redis

from common.constants import CMD_HOLD, CMD_RELEASE, CMD_COMMIT, SVC_PAYMENT
from common.messaging import publish_reply, start_participant_consumer
from common.models import (
    ParticipantCommand,
    ParticipantReply,
    decode_command,
    encode_reply,
)
from common.constants import PAYMENT_COMMANDS_QUEUE
import service as payment_service

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
    start_participant_consumer(_rabbitmq_url, PAYMENT_COMMANDS_QUEUE, _handle_command)


def _handle_command(channel, method, properties, body: bytes) -> None:
    try:
        cmd: ParticipantCommand = decode_command(body)
    except Exception as exc:
        logger.error("Failed to decode payment command: %s", exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    logger.info("payment cmd tx=%s command=%s", cmd.tx_id, cmd.command)
    reply = _dispatch(cmd)
    channel.basic_ack(delivery_tag=method.delivery_tag)

    try:
        publish_reply(_rabbitmq_url, cmd.reply_to, encode_reply(reply))
    except Exception as exc:
        logger.error("Failed to publish reply for tx=%s: %s", cmd.tx_id, exc)


def _dispatch(cmd: ParticipantCommand) -> ParticipantReply:
    tx_id = cmd.tx_id
    command = cmd.command

    try:
        if command == CMD_HOLD:
            if cmd.payment_payload is None:
                return _reply(cmd, ok=False, error="missing_payload")
            p = cmd.payment_payload
            result = payment_service.hold_payment(_db, tx_id, p.user_id, p.amount)
            return _reply(cmd, ok=result["ok"], error=result.get("error"))

        elif command == CMD_RELEASE:
            if cmd.payment_payload is None:
                return _reply(cmd, ok=False, error="missing_payload")
            p = cmd.payment_payload
            result = payment_service.release_payment(_db, tx_id, p.user_id, p.amount)
            ok = result["ok"] or result.get("error") == "tx_not_found"
            return _reply(cmd, ok=ok, error=None if ok else result.get("error"))

        elif command == CMD_COMMIT:
            result = payment_service.commit_payment(_db, tx_id)
            ok = result["ok"] or result.get("error") == "tx_not_found"
            return _reply(cmd, ok=ok, error=None if ok else result.get("error"))

        else:
            logger.warning("Unknown payment command: %s tx=%s", command, tx_id)
            return _reply(cmd, ok=False, error="unknown_command")

    except Exception as exc:
        logger.error("Unhandled error in payment dispatch tx=%s: %s", tx_id, exc)
        return _reply(cmd, ok=False, error="internal_error")


def _reply(cmd: ParticipantCommand, ok: bool, error: str | None) -> ParticipantReply:
    return ParticipantReply(
        tx_id=cmd.tx_id,
        service=SVC_PAYMENT,
        command=cmd.command,
        ok=ok,
        error=error,
    )
