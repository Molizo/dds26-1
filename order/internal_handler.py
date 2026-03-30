from common.constants import ORDER_CMD_MARK_PAID, ORDER_CMD_READ_ORDER
from common.models import InternalCommand, InternalReply
from store import mark_order_paid, read_order_snapshot


class OrderCommandHandler:
    def __init__(self, db) -> None:
        self._db = db

    def handle(self, cmd: InternalCommand) -> InternalReply:
        if cmd.command == ORDER_CMD_READ_ORDER:
            snapshot = read_order_snapshot(self._db, cmd.order_id)
            if snapshot is None:
                return InternalReply(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    ok=False,
                    error="not_found",
                )
            return InternalReply(
                request_id=cmd.request_id,
                command=cmd.command,
                ok=True,
                snapshot=snapshot,
            )

        if cmd.command == ORDER_CMD_MARK_PAID:
            if not cmd.tx_id:
                return InternalReply(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    ok=False,
                    error="tx_id_required",
                )
            marked = mark_order_paid(self._db, cmd.order_id)
            if not marked:
                return InternalReply(
                    request_id=cmd.request_id,
                    command=cmd.command,
                    ok=False,
                    error="not_found",
                )
            return InternalReply(request_id=cmd.request_id, command=cmd.command, ok=True)

        return InternalReply(
            request_id=cmd.request_id,
            command=cmd.command,
            ok=False,
            error="unknown_command",
        )
