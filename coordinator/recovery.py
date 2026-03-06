"""Background recovery worker for non-terminal checkout transactions.

The worker scans stale non-terminal transactions, then routes all repair
through CoordinatorService.resume_transaction(...). It performs:
1. One startup scan when the thread begins.
2. Periodic scans at a fixed interval.

This module is transport-agnostic and must not import Flask.
"""
import logging
import threading
import time
from typing import Optional

from common.constants import (
    ACTIVE_TX_GUARD_TTL,
    RECOVERY_SCAN_INTERVAL,
    RECOVERY_STALE_AGE,
    TERMINAL_STATUSES,
)
from coordinator.models import CheckoutTxValue
from coordinator.ports import TxStorePort
from coordinator.service import CoordinatorService

logger = logging.getLogger(__name__)


class RecoveryWorker:
    """Periodic scanner that resumes stale non-terminal transactions."""

    def __init__(
        self,
        coordinator: CoordinatorService,
        tx_store: TxStorePort,
        scan_interval_seconds: int = RECOVERY_SCAN_INTERVAL,
        stale_age_seconds: int = RECOVERY_STALE_AGE,
    ):
        self._coordinator = coordinator
        self._tx = tx_store
        self._scan_interval_seconds = max(1, int(scan_interval_seconds))
        self._stale_age_ms = max(0, int(stale_age_seconds)) * 1000
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> threading.Thread:
        """Start the background loop (startup scan + periodic scans)."""
        if self._thread and self._thread.is_alive():
            return self._thread

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="coordinator-recovery-worker",
        )
        self._thread.start()
        logger.info(
            "Recovery worker started (interval=%ss stale_age=%ss)",
            self._scan_interval_seconds,
            self._stale_age_ms // 1000,
        )
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background loop (used by tests/shutdown hooks)."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_scan_once(self, reason: str = "periodic") -> int:
        """Run one recovery scan and return how many txs were resumed."""
        now_ms = int(time.time() * 1000)
        recovered = 0

        try:
            txs = self._tx.get_non_terminal_txs()
        except Exception as exc:
            logger.error("Recovery scan (%s) failed to load tx list: %s", reason, exc)
            return 0

        for tx in txs:
            if tx.status in TERMINAL_STATUSES:
                continue
            if now_ms - tx.updated_at < self._stale_age_ms:
                continue

            if not self._acquire_recovery_lock(tx.tx_id):
                continue
            try:
                recovered += self._recover_one(tx, reason=reason)
            finally:
                self._release_recovery_lock(tx.tx_id)

        if recovered > 0:
            logger.info("Recovery scan (%s) resumed %s transaction(s)", reason, recovered)
        return recovered

    def _run(self) -> None:
        self.run_scan_once(reason="startup")
        while not self._stop_event.wait(self._scan_interval_seconds):
            self.run_scan_once(reason="periodic")

    def _recover_one(self, tx: CheckoutTxValue, reason: str) -> int:
        """Resume one stale tx if guards allow it. Returns 1 if resumed."""
        if not self._claim_active_guard(tx):
            return 0

        try:
            latest = self._tx.get_tx(tx.tx_id) or tx
            if latest.status in TERMINAL_STATUSES:
                self._clear_guard_if_owned(latest.order_id, latest.tx_id)
                return 0

            logger.info(
                "Recovery scan (%s) resuming tx=%s order=%s status=%s protocol=%s",
                reason,
                latest.tx_id,
                latest.order_id,
                latest.status,
                latest.protocol,
            )
            self._coordinator.resume_transaction(latest)
            self._finalize_guard_after_resume(latest.order_id, latest.tx_id)
            return 1
        except Exception:
            logger.exception("Recovery resume failed tx=%s", tx.tx_id)
            self._refresh_guard_if_owned(tx.order_id, tx.tx_id)
            return 0

    def _claim_active_guard(self, tx: CheckoutTxValue) -> bool:
        """Ensure this tx owns the active-tx guard before recovery work."""
        current_guard = self._tx.get_active_tx_guard(tx.order_id)
        if current_guard is None:
            acquired = self._tx.acquire_active_tx_guard(
                tx.order_id,
                tx.tx_id,
                ACTIVE_TX_GUARD_TTL,
            )
            if acquired:
                current_guard = tx.tx_id
            else:
                current_guard = self._tx.get_active_tx_guard(tx.order_id)

        if current_guard != tx.tx_id:
            return False

        self._tx.refresh_active_tx_guard(tx.order_id, ACTIVE_TX_GUARD_TTL)
        return True

    def _finalize_guard_after_resume(self, order_id: str, tx_id: str) -> None:
        tx_after = self._tx.get_tx(tx_id)
        if tx_after is not None and tx_after.status in TERMINAL_STATUSES:
            self._clear_guard_if_owned(order_id, tx_id)
            return
        self._refresh_guard_if_owned(order_id, tx_id)

    def _clear_guard_if_owned(self, order_id: str, tx_id: str) -> None:
        if self._tx.get_active_tx_guard(order_id) == tx_id:
            self._tx.clear_active_tx_guard(order_id)

    def _refresh_guard_if_owned(self, order_id: str, tx_id: str) -> None:
        if self._tx.get_active_tx_guard(order_id) == tx_id:
            self._tx.refresh_active_tx_guard(order_id, ACTIVE_TX_GUARD_TTL)

    def _acquire_recovery_lock(self, tx_id: str) -> bool:
        """Best-effort per-tx lock; falls back to always-true if unavailable."""
        fn = getattr(self._tx, "acquire_recovery_lock", None)
        if fn is None:
            return True
        try:
            return bool(fn(tx_id, ACTIVE_TX_GUARD_TTL))
        except Exception:
            logger.exception("Failed to acquire recovery lock tx=%s", tx_id)
            return False

    def _release_recovery_lock(self, tx_id: str) -> None:
        fn = getattr(self._tx, "release_recovery_lock", None)
        if fn is None:
            return
        try:
            fn(tx_id)
        except Exception:
            logger.exception("Failed to release recovery lock tx=%s", tx_id)


_worker_lock = threading.Lock()
_worker: Optional[RecoveryWorker] = None


def start_recovery_worker(
    coordinator: CoordinatorService,
    tx_store: TxStorePort,
    scan_interval_seconds: int = RECOVERY_SCAN_INTERVAL,
    stale_age_seconds: int = RECOVERY_STALE_AGE,
) -> RecoveryWorker:
    """Start a singleton recovery worker for the current process."""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_running():
            return _worker
        _worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            scan_interval_seconds=scan_interval_seconds,
            stale_age_seconds=stale_age_seconds,
        )
        _worker.start()
        return _worker

