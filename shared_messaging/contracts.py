from msgspec import Struct

from shared_messaging.types import MessageType

REQUIRED_METADATA_FIELDS: tuple[str, ...] = (
    "message_id",
    "saga_id",
    "order_id",
    "step",
    "attempt",
    "timestamp",
    "correlation_id",
    "causation_id",
)


class MessageMetadata(Struct, kw_only=True):
    message_id: str
    saga_id: str
    order_id: str
    step: str
    attempt: int
    timestamp: int
    correlation_id: str
    causation_id: str


class ItemQuantity(Struct, kw_only=True):
    item_id: str
    quantity: int


class CheckoutRequestedPayload(Struct, kw_only=True):
    user_id: str
    total_cost: int
    items: list[ItemQuantity]


class ReserveStockPayload(Struct, kw_only=True):
    items: list[ItemQuantity]


class StockReservedPayload(Struct, kw_only=True):
    items: list[ItemQuantity]


class StockRejectedPayload(Struct, kw_only=True):
    reason: str


class StockReleasedPayload(Struct, kw_only=True):
    items: list[ItemQuantity]


class ChargePaymentPayload(Struct, kw_only=True):
    user_id: str
    amount: int


class PaymentChargedPayload(Struct, kw_only=True):
    amount: int


class PaymentRejectedPayload(Struct, kw_only=True):
    reason: str


class ReleaseStockPayload(Struct, kw_only=True):
    items: list[ItemQuantity]


class OrderCommittedPayload(Struct, kw_only=True):
    paid: bool = True


class OrderFailedPayload(Struct, kw_only=True):
    reason: str


class DecodedMessage(Struct, kw_only=True):
    message_type: MessageType
    metadata: MessageMetadata
    payload: object


MESSAGE_PAYLOAD_TYPES: dict[str, type[Struct]] = {
    "CheckoutRequested": CheckoutRequestedPayload,
    "ReserveStock": ReserveStockPayload,
    "StockReserved": StockReservedPayload,
    "StockRejected": StockRejectedPayload,
    "StockReleased": StockReleasedPayload,
    "ChargePayment": ChargePaymentPayload,
    "PaymentCharged": PaymentChargedPayload,
    "PaymentRejected": PaymentRejectedPayload,
    "ReleaseStock": ReleaseStockPayload,
    "OrderCommitted": OrderCommittedPayload,
    "OrderFailed": OrderFailedPayload,
}
