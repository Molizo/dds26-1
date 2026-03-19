"""RabbitMQ-backed OrderPort for the standalone orchestrator."""
import uuid
from typing import Optional

from common.constants import ORDER_CMD_MARK_PAID, ORDER_CMD_READ_ORDER, ORDER_COMMANDS_QUEUE
from common.models import (
    InternalCommand,
    decode_internal_reply,
    encode_internal_command,
)
from common.rpc import rpc_request_bytes
from coordinator.ports import OrderPortUnavailable, OrderSnapshot


class RabbitMqOrderPort:
    def __init__(
        self,
        rabbitmq_url: str,
        timeout_seconds: float = 5.0,
    ):
        self._rabbitmq_url = rabbitmq_url
        self._timeout_seconds = timeout_seconds

    def read_order(self, order_id: str) -> Optional[OrderSnapshot]:
        request_id = str(uuid.uuid4())
        response = rpc_request_bytes(
            self._rabbitmq_url,
            ORDER_COMMANDS_QUEUE,
            request_id=request_id,
            body=encode_internal_command(
                InternalCommand(
                    request_id=request_id,
                    command=ORDER_CMD_READ_ORDER,
                    order_id=order_id,
                )
            ),
            timeout_seconds=self._timeout_seconds,
        )
        if response is None:
            raise OrderPortUnavailable("order_read_timeout")

        reply = decode_internal_reply(response)
        if not reply.ok or reply.snapshot is None:
            if reply.error == "not_found":
                return None
            raise OrderPortUnavailable(reply.error or "order_read_failed")

        snapshot = reply.snapshot
        return OrderSnapshot(
            order_id=snapshot.order_id,
            user_id=snapshot.user_id,
            total_cost=int(snapshot.total_cost),
            paid=bool(snapshot.paid),
            items=[(item_id, int(quantity)) for item_id, quantity in snapshot.items],
        )

    def mark_paid(self, order_id: str, tx_id: str) -> bool:
        request_id = str(uuid.uuid4())
        response = rpc_request_bytes(
            self._rabbitmq_url,
            ORDER_COMMANDS_QUEUE,
            request_id=request_id,
            body=encode_internal_command(
                InternalCommand(
                    request_id=request_id,
                    command=ORDER_CMD_MARK_PAID,
                    order_id=order_id,
                    tx_id=tx_id,
                )
            ),
            timeout_seconds=self._timeout_seconds,
        )
        if response is None:
            raise OrderPortUnavailable("mark_paid_timeout")

        reply = decode_internal_reply(response)
        if not reply.ok:
            if reply.error == "not_found":
                return False
            raise OrderPortUnavailable(reply.error or "mark_paid_failed")
        return True
