import os
import sys
import unittest

from msgspec import msgpack

os.environ.setdefault("GATEWAY_URL", "http://gateway:80")
os.environ.setdefault("PAYMENT_INTERNAL_URL", "http://payment-service:5000")
os.environ.setdefault("STOCK_INTERNAL_URL", "http://stock-service:5000")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "redis")
os.environ.setdefault("REDIS_DB", "0")

ORDER_DIR = os.path.join(os.path.dirname(__file__), "..", "order")
if ORDER_DIR not in sys.path:
    sys.path.insert(0, ORDER_DIR)

import recovery  # noqa: E402
from config import TERMINAL_TX_STATUSES  # noqa: E402
from models import CheckoutTxValue  # noqa: E402


class _FakeDb:
    def __init__(self):
        self._kv: dict[str, bytes] = {}

    def get(self, key: str):
        return self._kv.get(key)


class TestRecoveryEndpointStatus(unittest.TestCase):
    def test_recover_tx_by_id_returns_refreshed_status(self):
        tx_id = "unit-tx-1"
        key = f"{recovery.TX_KEY_PREFIX}{tx_id}"
        tx = CheckoutTxValue(
            tx_id=tx_id,
            order_id="order-1",
            user_id="user-1",
            total_cost=10,
            protocol="2pc",
            status="FAILED_NEEDS_RECOVERY",
            items_snapshot=[],
            stock_prepared=[],
            stock_committed=[],
            stock_compensated=[],
            payment_prepared=False,
            payment_committed=False,
            payment_reversed=False,
            decision=None,
            started_at_ms=0,
            updated_at_ms=0,
            last_error=None,
        )

        fake_db = _FakeDb()
        fake_db._kv[key] = msgpack.encode(tx)

        original_db = recovery.db
        original_recover_under_lock = recovery._recover_tx_under_lock
        try:
            recovery.db = fake_db

            def _fake_recover_under_lock(tx_entry: CheckoutTxValue):
                tx_entry.status = "COMPLETED"
                fake_db._kv[key] = msgpack.encode(tx_entry)

            recovery._recover_tx_under_lock = _fake_recover_under_lock
            refreshed_status = recovery.recover_tx_by_id(tx_id)
        finally:
            recovery.db = original_db
            recovery._recover_tx_under_lock = original_recover_under_lock

        self.assertEqual(refreshed_status, "COMPLETED")


class TestRecoveryStatusSemantics(unittest.TestCase):
    def _make_tx(self, status: str) -> CheckoutTxValue:
        return CheckoutTxValue(
            tx_id="unit-tx-status",
            order_id="order-1",
            user_id="user-1",
            total_cost=10,
            protocol="saga",
            status=status,
            items_snapshot=[],
            stock_prepared=[],
            stock_committed=[],
            stock_compensated=[],
            payment_prepared=False,
            payment_committed=False,
            payment_reversed=False,
            decision=None,
            started_at_ms=0,
            updated_at_ms=0,
            last_error=None,
        )

    def test_failed_needs_recovery_is_not_terminal(self):
        self.assertFalse(recovery.STATUS_FAILED_NEEDS_RECOVERY in TERMINAL_TX_STATUSES)

    def test_recover_tx_keeps_active_tx_for_failed_needs_recovery(self):
        tx = self._make_tx(recovery.STATUS_FAILED_NEEDS_RECOVERY)
        clear_calls: list[str] = []

        original_fetch_order = recovery._fetch_order
        original_recover_saga_tx = recovery._recover_saga_tx
        original_clear_active = recovery._clear_active_tx_best_effort
        try:
            recovery._fetch_order = lambda _order_id: None

            def _fake_recover_saga(tx_entry: CheckoutTxValue, _order_entry):
                tx_entry.status = recovery.STATUS_FAILED_NEEDS_RECOVERY

            recovery._recover_saga_tx = _fake_recover_saga
            recovery._clear_active_tx_best_effort = lambda order_id: clear_calls.append(order_id)

            recovery.recover_tx(tx)
        finally:
            recovery._fetch_order = original_fetch_order
            recovery._recover_saga_tx = original_recover_saga_tx
            recovery._clear_active_tx_best_effort = original_clear_active

        self.assertEqual(clear_calls, [])


if __name__ == "__main__":
    unittest.main()
