"""Abstract port interfaces for the coordinator.

The coordinator accesses order domain state and tx persistence exclusively
through these interfaces. In Phase 1, implementations are direct Redis calls
in order/store.py. In Phase 2, implementations swap to HTTP calls on the
order service — coordinator protocol code does not change.

These are defined as typing.Protocol so implementations need not inherit;
duck typing is sufficient.
"""
from typing import Optional, Protocol

from common.models import OrderSnapshot
from coordinator.models import CheckoutTxValue


class OrderPortUnavailable(RuntimeError):
    """Raised when the order service cannot confirm an RPC result."""


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

    def get_stale_non_terminal_txs(
        self, stale_before_ms: int, batch_limit: int = 50,
    ) -> list[CheckoutTxValue]: ...

    def set_decision(self, tx_id: str, decision: str) -> None: ...

    def set_decision_and_update_tx(
        self, tx_id: str, decision: str, tx: CheckoutTxValue,
    ) -> None: ...

    def get_decision(self, tx_id: str) -> Optional[str]: ...

    def set_commit_fence(self, order_id: str, tx_id: str) -> None: ...

    def set_decision_fence_and_update_tx(
        self, tx_id: str, decision: str, order_id: str, tx: CheckoutTxValue,
    ) -> None: ...

    def get_commit_fence(self, order_id: str) -> Optional[str]: ...

    def clear_commit_fence(self, order_id: str) -> None: ...

    def acquire_active_tx_guard(self, order_id: str, tx_id: str, ttl: int) -> bool: ...

    def get_active_tx_guard(self, order_id: str) -> Optional[str]: ...

    def clear_active_tx_guard(self, order_id: str) -> None: ...

    def clear_active_tx_guard_if_owned(self, order_id: str, tx_id: str) -> bool: ...

    def refresh_active_tx_guard(self, order_id: str, ttl: int) -> bool: ...

    def acquire_mutation_guard(self, order_id: str, lease_id: str, ttl: int) -> bool: ...

    def get_mutation_guard(self, order_id: str) -> Optional[str]: ...

    def release_mutation_guard(self, order_id: str, lease_id: str) -> bool: ...

    # Optional but recommended: best-effort per-tx recovery lock to avoid
    # duplicate recovery across multiple scanner threads/processes.
    def acquire_recovery_lock(self, tx_id: str, ttl: int) -> bool: ...

    def release_recovery_lock(self, tx_id: str) -> None: ...

    # Optional but recommended in Phase 2 when multiple orchestrator instances
    # exist: only the current leader runs periodic recovery scans.
    def acquire_recovery_leader(self, owner_id: str, ttl: int) -> bool: ...

    def release_recovery_leader(self, owner_id: str) -> None: ...
