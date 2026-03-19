"""RabbitMQ consumer for internal order-service commands."""
import logging

from common.constants import ORDER_CMD_MARK_PAID, ORDER_CMD_READ_ORDER, ORDER_COMMANDS_QUEUE
from common.messaging import start_participant_consumer
from common.models import (
    InternalCommand,
    decode_internal_command,
    encode_internal_reply,
)
from common.worker_support import handle_internal_rpc_delivery
from internal_handler import OrderCommandHandler

logger = logging.getLogger(__name__)

_rabbitmq_url: str | None = None
_handler: OrderCommandHandler | None = None


def init_worker(db, rabbitmq_url: str) -> None:
    global _rabbitmq_url, _handler
    _rabbitmq_url = rabbitmq_url
    _handler = OrderCommandHandler(db)


def start_consumer_thread() -> None:
    if _handler is None or _rabbitmq_url is None:
        raise RuntimeError("init_worker() must be called before start_consumer_thread()")
    start_participant_consumer(_rabbitmq_url, ORDER_COMMANDS_QUEUE, _handle_command)


def _handle_command(channel, method, properties, body: bytes) -> None:
    handle_internal_rpc_delivery(
        channel=channel,
        method=method,
        properties=properties,
        body=body,
        rabbitmq_url=_rabbitmq_url,
        service_name="order",
        decode_command=decode_internal_command,
        dispatch=_dispatch,
        encode_reply=encode_internal_reply,
        logger=logger,
    )


def _dispatch(cmd: InternalCommand):
    if _handler is None:
        raise RuntimeError("init_worker() must be called before command dispatch")
    reply = _handler.handle(cmd)
    if cmd.command == ORDER_CMD_MARK_PAID and reply.ok:
        logger.info("Order marked paid order=%s tx=%s", cmd.order_id, cmd.tx_id)
    elif cmd.command not in {ORDER_CMD_READ_ORDER, ORDER_CMD_MARK_PAID}:
        logger.warning("Unknown order command=%s request=%s", cmd.command, cmd.request_id)
    return reply
