from typing import Final, Literal

MessageType = Literal[
    "CheckoutRequested",
    "ReserveStock",
    "StockReserved",
    "StockRejected",
    "ChargePayment",
    "PaymentCharged",
    "PaymentRejected",
    "ReleaseStock",
    "OrderCommitted",
    "OrderFailed",
]

SUPPORTED_MESSAGE_TYPES: Final[tuple[MessageType, ...]] = (
    "CheckoutRequested",
    "ReserveStock",
    "StockReserved",
    "StockRejected",
    "ChargePayment",
    "PaymentCharged",
    "PaymentRejected",
    "ReleaseStock",
    "OrderCommitted",
    "OrderFailed",
)
