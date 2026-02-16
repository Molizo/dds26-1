import time

from msgspec import Struct, msgpack

from shared_messaging.contracts import ItemQuantity

ORDER_STATUS_NOT_STARTED = "NOT_STARTED"
ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_COMPLETED = "COMPLETED"
ORDER_STATUS_FAILED = "FAILED"

SAGA_STATE_PENDING = "PENDING"
SAGA_STATE_RESERVING_STOCK = "RESERVING_STOCK"
SAGA_STATE_CHARGING_PAYMENT = "CHARGING_PAYMENT"
SAGA_STATE_RELEASING_STOCK = "RELEASING_STOCK"
SAGA_STATE_COMMITTING_ORDER = "COMMITTING_ORDER"
SAGA_STATE_COMPLETED = "COMPLETED"
SAGA_STATE_FAILED = "FAILED"

SAGA_TERMINAL_STATES = {SAGA_STATE_COMPLETED, SAGA_STATE_FAILED}


class OrderValue(Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int
    checkout_status: str = ORDER_STATUS_NOT_STARTED
    checkout_failure_reason: str | None = None


class SagaValue(Struct):
    saga_id: str
    order_id: str
    user_id: str
    items: list[ItemQuantity]
    total_cost: int
    state: str
    failure_reason: str | None = None
    updated_at_ms: int = 0


def now_ms() -> int:
    return int(time.time() * 1000)


def encode_order(order: OrderValue) -> bytes:
    return msgpack.encode(order)


def decode_order(raw: bytes | None) -> OrderValue | None:
    if raw is None:
        return None
    return msgpack.decode(raw, type=OrderValue)


def encode_saga(saga: SagaValue) -> bytes:
    return msgpack.encode(saga)


def decode_saga(raw: bytes | None) -> SagaValue | None:
    if raw is None:
        return None
    return msgpack.decode(raw, type=SagaValue)

