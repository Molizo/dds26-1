"""Abstract port interfaces for the coordinator.

The coordinator accesses order domain state and tx persistence exclusively
through these interfaces. In Phase 1, implementations are direct Redis calls
in order/store.py. In Phase 2, implementations swap to HTTP calls on the
order service — coordinator protocol code does not change.

These are defined as typing.Protocol so implementations need not inherit;
duck typing is sufficient.
"""
from typing import Optional, Protocol

from coordinator.models import CheckoutTxValue


class OrderSnapshot:
    """Lightweight snapshot of order state needed by the coordinator."""
    __slots__ = ("order_id", "user_id", "total_cost", "paid", "items")

    def __init__(
        self,
        order_id: str,
        user_id: str,
        total_cost: int,
        paid: bool,
        items: list[tuple[str, int]],
    ):
        self.order_id = order_id
        self.user_id = user_id
        self.total_cost = total_cost
        self.paid = paid
        self.items = items


class OrderPort(Protocol):
    """Read and update order domain records."""

    def read_order(self, order_id: str) -> Optional[OrderSnapshot]:
        """Return the current order state, or None if not found."""
        ...

    def mark_paid(self, order_id: str, tx_id: str) -> bool:
        """Idempotently mark the order as paid. Returns True on success."""
        ...


class TxStorePort(Protocol):
    """Coordinator transaction state persistence."""

    def create_tx(self, tx: CheckoutTxValue) -> None: ...

    def update_tx(self, tx: CheckoutTxValue) -> None: ...

    def get_tx(self, tx_id: str) -> Optional[CheckoutTxValue]: ...

    def get_non_terminal_txs(self) -> list[CheckoutTxValue]: ...

    def set_decision(self, tx_id: str, decision: str) -> None: ...

    def get_decision(self, tx_id: str) -> Optional[str]: ...

    def set_commit_fence(self, order_id: str, tx_id: str) -> None: ...

    def get_commit_fence(self, order_id: str) -> Optional[str]: ...

    def clear_commit_fence(self, order_id: str) -> None: ...

    def acquire_active_tx_guard(self, order_id: str, tx_id: str, ttl: int) -> bool: ...

    def get_active_tx_guard(self, order_id: str) -> Optional[str]: ...

    def clear_active_tx_guard(self, order_id: str) -> None: ...

    def refresh_active_tx_guard(self, order_id: str, ttl: int) -> bool: ...
