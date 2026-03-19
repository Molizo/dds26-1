"""RabbitMQ consumer for internal orchestrator commands."""
import logging

from common.messaging import publish_message, start_participant_consumer
from common.constants import (
    ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD,
    ORCHESTRATOR_CMD_CHECKOUT,
    ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD,
    ORCHESTRATOR_COMMANDS_QUEUE,
)
from common.models import (
    InternalCommand,
    InternalReply,
    decode_internal_command,
    encode_internal_reply,
)

logger = logging.getLogger(__name__)

_rabbitmq_url: str | None = None


def init_worker(rabbitmq_url: str) -> None:
    global _rabbitmq_url
    _rabbitmq_url = rabbitmq_url


def start_consumer_thread() -> None:
    if _rabbitmq_url is None:
        raise RuntimeError("init_worker() must be called before start_consumer_thread()")
    start_participant_consumer(_rabbitmq_url, ORCHESTRATOR_COMMANDS_QUEUE, _handle_command)


def _handle_command(channel, method, properties, body: bytes) -> None:
    try:
        cmd: InternalCommand = decode_internal_command(body)
    except Exception as exc:
        logger.error("Failed to decode orchestrator command: %s", exc)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    reply_to = properties.reply_to if properties else None
    correlation_id = properties.correlation_id if properties else None
    if not reply_to or not correlation_id:
        logger.error("Dropping orchestrator command without reply metadata request=%s", cmd.request_id)
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
        logger.error("Failed to publish orchestrator reply request=%s: %s", cmd.request_id, exc)


def _dispatch(cmd: InternalCommand) -> InternalReply:
    try:
        from app import (
            execute_checkout_command,
            acquire_mutation_guard_command,
            release_mutation_guard_command,
        )
    except ModuleNotFoundError:
        from orchestrator.app import (
            execute_checkout_command,
            acquire_mutation_guard_command,
            release_mutation_guard_command,
        )

    if cmd.command == ORCHESTRATOR_CMD_CHECKOUT:
        if not cmd.tx_id:
            return InternalReply(
                request_id=cmd.request_id,
                command=cmd.command,
                ok=False,
                status_code=400,
                error="tx_id is required",
            )
        result = execute_checkout_command(cmd.order_id, cmd.tx_id)
        return InternalReply(
            request_id=cmd.request_id,
            command=cmd.command,
            ok=result.success,
            status_code=result.status_code,
            error=result.error,
        )

    if cmd.command == ORCHESTRATOR_CMD_ACQUIRE_MUTATION_GUARD:
        if not cmd.lease_id:
            return InternalReply(
                request_id=cmd.request_id,
                command=cmd.command,
                ok=False,
                status_code=400,
                error="lease_id is required",
            )
        acquired, reason, status_code = acquire_mutation_guard_command(cmd.order_id, cmd.lease_id)
        return InternalReply(
            request_id=cmd.request_id,
            command=cmd.command,
            ok=acquired,
            status_code=status_code,
            reason=reason,
            error=None if acquired else reason,
        )

    if cmd.command == ORCHESTRATOR_CMD_RELEASE_MUTATION_GUARD:
        if not cmd.lease_id:
            return InternalReply(
                request_id=cmd.request_id,
                command=cmd.command,
                ok=False,
                status_code=400,
                error="lease_id is required",
            )
        released = release_mutation_guard_command(cmd.order_id, cmd.lease_id)
        return InternalReply(
            request_id=cmd.request_id,
            command=cmd.command,
            ok=released,
            status_code=200,
        )

    logger.warning("Unknown orchestrator command=%s request=%s", cmd.command, cmd.request_id)
    return InternalReply(
        request_id=cmd.request_id,
        command=cmd.command,
        ok=False,
        status_code=400,
        error="unknown_command",
    )
