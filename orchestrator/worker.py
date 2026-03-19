"""RabbitMQ consumer for internal orchestrator commands."""
import logging

from common.messaging import start_participant_consumer
from common.constants import (
    ORCHESTRATOR_COMMANDS_QUEUE,
)
from common.models import (
    InternalCommand,
    decode_internal_command,
    encode_internal_reply,
)
from common.worker_support import handle_internal_rpc_delivery

logger = logging.getLogger(__name__)

_rabbitmq_url: str | None = None
_runtime = None


def init_worker(runtime, rabbitmq_url: str) -> None:
    global _rabbitmq_url, _runtime
    _rabbitmq_url = rabbitmq_url
    _runtime = runtime


def start_consumer_thread() -> None:
    if _rabbitmq_url is None or _runtime is None:
        raise RuntimeError("init_worker() must be called before start_consumer_thread()")
    start_participant_consumer(_rabbitmq_url, ORCHESTRATOR_COMMANDS_QUEUE, _handle_command)


def _handle_command(channel, method, properties, body: bytes) -> None:
    handle_internal_rpc_delivery(
        channel=channel,
        method=method,
        properties=properties,
        body=body,
        rabbitmq_url=_rabbitmq_url,
        service_name="orchestrator",
        decode_command=decode_internal_command,
        dispatch=_dispatch,
        encode_reply=encode_internal_reply,
        logger=logger,
    )


def _dispatch(cmd: InternalCommand):
    if _runtime is None:
        raise RuntimeError("init_worker() must be called before command dispatch")
    reply = _runtime.handle_internal_command(cmd)
    if reply.error == "unknown_command":
        logger.warning("Unknown orchestrator command=%s request=%s", cmd.command, cmd.request_id)
    return reply
