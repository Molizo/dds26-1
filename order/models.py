from msgspec import Struct


class OrderValue(Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int


class CheckoutTxValue(Struct):
    tx_id: str
    order_id: str
    user_id: str
    total_cost: int
    protocol: str
    status: str
    items_snapshot: list[tuple[str, int]]
    stock_prepared: list[tuple[str, int]]
    stock_committed: list[tuple[str, int]]
    stock_compensated: list[tuple[str, int]]
    payment_prepared: bool
    payment_committed: bool
    payment_reversed: bool
    decision: str | None
    started_at_ms: int
    updated_at_ms: int
    last_error: str | None

