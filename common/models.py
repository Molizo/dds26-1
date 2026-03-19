"""RabbitMQ message definitions shared by all services.

All messages are encoded with msgspec.msgpack. Struct fields are array-encoded
by default, so field order matters — do not reorder fields without migrating
all producers and consumers simultaneously.
"""
import msgspec
from typing import Optional


class StockHoldPayload(msgspec.Struct):
    """Items to hold/release in stock service. One instance per transaction."""
    # List of (item_id, quantity) pairs — pre-aggregated by coordinator.
    items: list[tuple[str, int]]


class PaymentHoldPayload(msgspec.Struct):
    """Funds to hold/release in payment service. One instance per transaction."""
    user_id: str
    amount: int


class OrderSnapshotPayload(msgspec.Struct):
    """Order state transferred over internal RPC."""
    order_id: str
    user_id: str
    total_cost: int
    paid: bool
    items: list[tuple[str, int]]


class ParticipantCommand(msgspec.Struct):
    """Coordinator → participant command message."""
    tx_id: str
    # One of CMD_HOLD, CMD_RELEASE, CMD_COMMIT from common.constants
    command: str
    # Name of the exclusive reply queue for this coordinator process
    reply_to: str
    # Exactly one payload field is populated based on which service receives the command
    stock_payload: Optional[StockHoldPayload] = None
    payment_payload: Optional[PaymentHoldPayload] = None


class ParticipantReply(msgspec.Struct):
    """Participant → coordinator reply message."""
    tx_id: str
    # "stock" or "payment"
    service: str
    # Echoes the command that was processed
    command: str
    ok: bool
    # Error code when ok=False. Values: not_found, insufficient_stock,
    # insufficient_credit, already_held, already_released, already_committed,
    # tx_not_found
    error: Optional[str] = None


class InternalCommand(msgspec.Struct):
    """Internal RabbitMQ RPC command between order-service and orchestrator."""
    request_id: str
    command: str
    order_id: str
    tx_id: Optional[str] = None
    lease_id: Optional[str] = None


class InternalReply(msgspec.Struct):
    """Internal RabbitMQ RPC reply between order-service and orchestrator."""
    request_id: str
    command: str
    ok: bool
    error: Optional[str] = None
    snapshot: Optional[OrderSnapshotPayload] = None
    status_code: Optional[int] = None
    reason: Optional[str] = None


# Pre-built encoder/decoder instances for efficient reuse
_encoder = msgspec.msgpack.Encoder()
_cmd_decoder = msgspec.msgpack.Decoder(ParticipantCommand)
_reply_decoder = msgspec.msgpack.Decoder(ParticipantReply)
_internal_cmd_decoder = msgspec.msgpack.Decoder(InternalCommand)
_internal_reply_decoder = msgspec.msgpack.Decoder(InternalReply)


def encode_command(cmd: ParticipantCommand) -> bytes:
    return _encoder.encode(cmd)


def decode_command(data: bytes) -> ParticipantCommand:
    return _cmd_decoder.decode(data)


def encode_reply(reply: ParticipantReply) -> bytes:
    return _encoder.encode(reply)


def decode_reply(data: bytes) -> ParticipantReply:
    return _reply_decoder.decode(data)


def encode_internal_command(cmd: InternalCommand) -> bytes:
    return _encoder.encode(cmd)


def decode_internal_command(data: bytes) -> InternalCommand:
    return _internal_cmd_decoder.decode(data)


def encode_internal_reply(reply: InternalReply) -> bytes:
    return _encoder.encode(reply)


def decode_internal_reply(data: bytes) -> InternalReply:
    return _internal_reply_decoder.decode(data)
