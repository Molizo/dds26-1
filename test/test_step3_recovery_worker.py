"""Unit tests for Step 3: recovery worker scanning behavior."""
import os
import sys
import time
import unittest

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from common.constants import (
    PROTOCOL_SAGA,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED_NEEDS_RECOVERY,
    STATUS_HOLDING,
    TERMINAL_STATUSES,
)
from common.result import CheckoutResult
from coordinator.models import make_tx
from coordinator.recovery import RecoveryWorker


class _MockTxStore:
    def __init__(self):
        self.txs = {}
        self.guards = {}
        self.recovery_locks = set()
        self.refresh_calls = []

    def create_tx(self, tx):
        self.txs[tx.tx_id] = tx

    def update_tx(self, tx):
        self.txs[tx.tx_id] = tx

    def get_tx(self, tx_id):
        return self.txs.get(tx_id)

    def get_stale_non_terminal_txs(self, stale_before_ms, batch_limit=50):
        return [
            tx for tx in self.txs.values()
            if tx.status not in TERMINAL_STATUSES and tx.updated_at <= stale_before_ms
        ]

    def acquire_active_tx_guard(self, order_id, tx_id, ttl):
        if order_id in self.guards:
            return False
        self.guards[order_id] = tx_id
        return True

    def get_active_tx_guard(self, order_id):
        return self.guards.get(order_id)

    def clear_active_tx_guard(self, order_id):
        self.guards.pop(order_id, None)

    def refresh_active_tx_guard(self, order_id, ttl):
        if order_id not in self.guards:
            return False
        self.refresh_calls.append((order_id, ttl))
        return True

    def acquire_recovery_lock(self, tx_id, ttl):
        if tx_id in self.recovery_locks:
            return False
        self.recovery_locks.add(tx_id)
        return True

    def release_recovery_lock(self, tx_id):
        self.recovery_locks.discard(tx_id)


class _MockCoordinator:
    def __init__(self, tx_store: _MockTxStore, next_status: str):
        self._tx_store = tx_store
        self._next_status = next_status
        self.calls = []

    def resume_transaction(self, tx):
        self.calls.append(tx.tx_id)
        tx.status = self._next_status
        self._tx_store.update_tx(tx)
        if self._next_status == STATUS_COMPLETED:
            return CheckoutResult.ok()
        if self._next_status in TERMINAL_STATUSES:
            return CheckoutResult.fail("aborted")
        return CheckoutResult.fail("still_recovering")


def _make_stale_tx(
    tx_id: str,
    order_id: str,
    status: str,
    stale_seconds: int = 120,
):
    tx = make_tx(
        tx_id=tx_id,
        order_id=order_id,
        user_id="user-1",
        total_cost=100,
        protocol=PROTOCOL_SAGA,
        items_snapshot=[("item-1", 1)],
        status=status,
    )
    tx.updated_at -= stale_seconds * 1000
    return tx


class TestRecoveryWorker(unittest.TestCase):

    def test_startup_scan_resumes_stale_tx_and_clears_guard_on_terminal(self):
        tx_store = _MockTxStore()
        tx = _make_stale_tx("tx-1", "order-1", STATUS_FAILED_NEEDS_RECOVERY)
        tx_store.create_tx(tx)
        tx_store.guards["order-1"] = "tx-1"
        coordinator = _MockCoordinator(tx_store, next_status=STATUS_ABORTED)

        worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            stale_age_seconds=0,
        )
        recovered = worker.run_scan_once(reason="startup")

        self.assertEqual(recovered, 1)
        self.assertEqual(coordinator.calls, ["tx-1"])
        self.assertEqual(tx_store.get_tx("tx-1").status, STATUS_ABORTED)
        self.assertNotIn("order-1", tx_store.guards)

    def test_scan_skips_fresh_transaction(self):
        tx_store = _MockTxStore()
        tx = _make_stale_tx("tx-2", "order-2", STATUS_HOLDING, stale_seconds=0)
        tx_store.create_tx(tx)
        coordinator = _MockCoordinator(tx_store, next_status=STATUS_COMPLETED)

        worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            stale_age_seconds=30,
        )
        recovered = worker.run_scan_once(reason="periodic")

        self.assertEqual(recovered, 0)
        self.assertEqual(coordinator.calls, [])

    def test_scan_skips_when_order_guard_belongs_to_other_tx(self):
        tx_store = _MockTxStore()
        tx = _make_stale_tx("tx-3", "order-3", STATUS_HOLDING)
        tx_store.create_tx(tx)
        tx_store.guards["order-3"] = "tx-other"
        coordinator = _MockCoordinator(tx_store, next_status=STATUS_COMPLETED)

        worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            stale_age_seconds=0,
        )
        recovered = worker.run_scan_once(reason="periodic")

        self.assertEqual(recovered, 0)
        self.assertEqual(coordinator.calls, [])
        self.assertEqual(tx_store.guards["order-3"], "tx-other")

    def test_duplicate_scans_do_not_recover_terminal_twice(self):
        tx_store = _MockTxStore()
        tx = _make_stale_tx("tx-4", "order-4", STATUS_FAILED_NEEDS_RECOVERY)
        tx_store.create_tx(tx)
        tx_store.guards["order-4"] = "tx-4"
        coordinator = _MockCoordinator(tx_store, next_status=STATUS_COMPLETED)

        worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            stale_age_seconds=0,
        )
        first = worker.run_scan_once(reason="startup")
        second = worker.run_scan_once(reason="periodic")

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(coordinator.calls.count("tx-4"), 1)

    def test_non_terminal_resume_keeps_guard(self):
        tx_store = _MockTxStore()
        tx = _make_stale_tx("tx-5", "order-5", STATUS_HOLDING)
        tx_store.create_tx(tx)
        tx_store.guards["order-5"] = "tx-5"
        coordinator = _MockCoordinator(tx_store, next_status=STATUS_FAILED_NEEDS_RECOVERY)

        worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            stale_age_seconds=0,
        )
        recovered = worker.run_scan_once(reason="periodic")

        self.assertEqual(recovered, 1)
        self.assertEqual(coordinator.calls, ["tx-5"])
        self.assertEqual(tx_store.get_tx("tx-5").status, STATUS_FAILED_NEEDS_RECOVERY)
        self.assertEqual(tx_store.guards.get("order-5"), "tx-5")
        self.assertGreaterEqual(len(tx_store.refresh_calls), 1)

    def test_scan_skips_when_recovery_lock_is_already_held(self):
        tx_store = _MockTxStore()
        tx = _make_stale_tx("tx-6", "order-6", STATUS_HOLDING)
        tx_store.create_tx(tx)
        tx_store.recovery_locks.add("tx-6")
        coordinator = _MockCoordinator(tx_store, next_status=STATUS_COMPLETED)

        worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            stale_age_seconds=0,
        )
        recovered = worker.run_scan_once(reason="periodic")

        self.assertEqual(recovered, 0)
        self.assertEqual(coordinator.calls, [])

    def test_scan_continues_when_guard_lookup_raises(self):
        class _FlakyGuardTxStore(_MockTxStore):
            def __init__(self):
                super().__init__()
                self._fail_once_order_ids = {"order-err"}

            def get_active_tx_guard(self, order_id):
                if order_id in self._fail_once_order_ids:
                    self._fail_once_order_ids.remove(order_id)
                    raise RuntimeError("redis temporarily unavailable")
                return super().get_active_tx_guard(order_id)

        tx_store = _FlakyGuardTxStore()
        tx_store.create_tx(_make_stale_tx("tx-err", "order-err", STATUS_HOLDING))
        tx_store.create_tx(_make_stale_tx("tx-ok", "order-ok", STATUS_HOLDING))
        coordinator = _MockCoordinator(tx_store, next_status=STATUS_COMPLETED)

        worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            stale_age_seconds=0,
        )
        recovered = worker.run_scan_once(reason="periodic")

        self.assertEqual(recovered, 1)
        self.assertEqual(coordinator.calls, ["tx-ok"])

    def test_scan_revalidates_staleness_after_lock_acquire(self):
        class _FresheningLockTxStore(_MockTxStore):
            def acquire_recovery_lock(self, tx_id, ttl):
                acquired = super().acquire_recovery_lock(tx_id, ttl)
                if acquired:
                    tx = self.txs[tx_id]
                    tx.updated_at = int(time.time() * 1000)
                    self.update_tx(tx)
                return acquired

        tx_store = _FresheningLockTxStore()
        tx = _make_stale_tx("tx-7", "order-7", STATUS_HOLDING, stale_seconds=120)
        tx_store.create_tx(tx)
        tx_store.guards["order-7"] = "tx-7"
        coordinator = _MockCoordinator(tx_store, next_status=STATUS_FAILED_NEEDS_RECOVERY)

        worker = RecoveryWorker(
            coordinator=coordinator,
            tx_store=tx_store,
            stale_age_seconds=30,
        )
        recovered = worker.run_scan_once(reason="periodic")

        self.assertEqual(recovered, 0)
        self.assertEqual(coordinator.calls, [])


if __name__ == "__main__":
    unittest.main()
