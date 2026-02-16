import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "redis")
os.environ.setdefault("REDIS_DB", "0")

from order.models import (
    SAGA_STATE_COMMITTING_ORDER,
    SAGA_STATE_PENDING,
    SAGA_STATE_RESERVING_STOCK,
    SagaValue,
)
from order.workers import reconciliation_worker
from shared_messaging.contracts import ItemQuantity


class TestPhase6ReconciliationWorker(unittest.TestCase):
    def _build_saga(self, *, state: str, updated_at_ms: int = 0) -> SagaValue:
        return SagaValue(
            saga_id="saga-1",
            order_id="order-1",
            user_id="user-1",
            items=[ItemQuantity(item_id="item-1", quantity=2)],
            total_cost=40,
            state=state,
            updated_at_ms=updated_at_ms,
        )

    def test_recovery_message_id_is_deterministic(self):
        self.assertEqual(
            reconciliation_worker._recovery_message_id("s1", "recover_reserve_stock"),
            "recovery:s1:recover_reserve_stock",
        )
        self.assertEqual(
            reconciliation_worker._recovery_message_id("s1", "recover_reserve_stock"),
            "recovery:s1:recover_reserve_stock",
        )

    @patch("order.workers.reconciliation_worker._touch_saga")
    @patch("order.workers.reconciliation_worker._publish_for_state")
    @patch("order.workers.reconciliation_worker._iter_sagas")
    def test_reconcile_once_pending_publishes_step(
        self,
        mock_iter_sagas,
        mock_publish,
        mock_touch,
    ):
        redis_client = MagicMock()
        redis_client.set.return_value = True
        channel = MagicMock()
        saga = self._build_saga(state=SAGA_STATE_PENDING, updated_at_ms=0)
        mock_iter_sagas.return_value = [saga]

        stats = reconciliation_worker.reconcile_once(
            redis_client,
            channel,
            stale_after_ms=1000,
            batch_size=10,
            step_lock_ttl_sec=120,
            max_actions=10,
        )

        mock_publish.assert_called_once_with(channel, saga, step="recover_checkout_requested")
        mock_touch.assert_called_once_with(redis_client, saga)
        self.assertEqual(stats["reconciled"], 1)
        self.assertEqual(stats["errors"], 0)

    @patch("order.workers.reconciliation_worker._complete_committing_order")
    @patch("order.workers.reconciliation_worker._publish_for_state")
    @patch("order.workers.reconciliation_worker._iter_sagas")
    def test_reconcile_once_committing_order_completes_locally(
        self,
        mock_iter_sagas,
        mock_publish,
        mock_complete,
    ):
        redis_client = MagicMock()
        redis_client.set.return_value = True
        channel = MagicMock()
        saga = self._build_saga(state=SAGA_STATE_COMMITTING_ORDER, updated_at_ms=0)
        mock_iter_sagas.return_value = [saga]

        stats = reconciliation_worker.reconcile_once(
            redis_client,
            channel,
            stale_after_ms=1000,
            batch_size=10,
            step_lock_ttl_sec=120,
            max_actions=10,
        )

        mock_complete.assert_called_once_with(redis_client, saga)
        mock_publish.assert_not_called()
        self.assertEqual(stats["reconciled"], 1)

    @patch("order.workers.reconciliation_worker._iter_sagas")
    def test_reconcile_once_skips_when_step_lock_held(self, mock_iter_sagas):
        redis_client = MagicMock()
        redis_client.set.return_value = False
        channel = MagicMock()
        saga = self._build_saga(state=SAGA_STATE_RESERVING_STOCK, updated_at_ms=0)
        mock_iter_sagas.return_value = [saga]

        stats = reconciliation_worker.reconcile_once(
            redis_client,
            channel,
            stale_after_ms=1000,
            batch_size=10,
            step_lock_ttl_sec=120,
            max_actions=10,
        )

        self.assertEqual(stats["locked_out"], 1)
        self.assertEqual(stats["reconciled"], 0)

    @patch("order.workers.reconciliation_worker._release_leader_lock")
    @patch("order.workers.reconciliation_worker.reconcile_once", return_value={"reconciled": 1})
    @patch("order.workers.reconciliation_worker._acquire_leader_lock", return_value=True)
    def test_run_startup_recovery_once_runs_and_releases_lock(
        self,
        _mock_acquire,
        mock_reconcile_once,
        mock_release,
    ):
        result = reconciliation_worker.run_startup_recovery_once(
            MagicMock(),
            MagicMock(),
            stale_after_ms=1000,
            batch_size=10,
            step_lock_ttl_sec=120,
            max_actions=10,
            leader_lock_ttl_ms=5000,
            owner_token="owner-1",
        )

        mock_reconcile_once.assert_called_once()
        mock_release.assert_called_once()
        self.assertEqual(result, {"reconciled": 1})

    @patch("order.workers.reconciliation_worker._release_leader_lock")
    @patch("order.workers.reconciliation_worker.reconcile_once")
    @patch("order.workers.reconciliation_worker._acquire_leader_lock", return_value=False)
    def test_run_startup_recovery_once_skips_without_lock(
        self,
        _mock_acquire,
        mock_reconcile_once,
        mock_release,
    ):
        result = reconciliation_worker.run_startup_recovery_once(
            MagicMock(),
            MagicMock(),
            stale_after_ms=1000,
            batch_size=10,
            step_lock_ttl_sec=120,
            max_actions=10,
            leader_lock_ttl_ms=5000,
            owner_token="owner-1",
        )

        self.assertIsNone(result)
        mock_reconcile_once.assert_not_called()
        mock_release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
