import os
import sys
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

os.environ.setdefault("GATEWAY_URL", "http://gateway:80")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "redis")
os.environ.setdefault("REDIS_DB", "0")

from order import app as order_app_module
from order.models import SAGA_STATE_COMPLETED, SAGA_STATE_PENDING, OrderValue, SagaValue


class TestPhase5CheckoutOrchestration(unittest.TestCase):
    def setUp(self):
        self.client = order_app_module.app.test_client()

    @patch("order.app.wait_for_terminal_saga")
    @patch("order.app.publish_checkout_requested")
    @patch("order.app.ensure_saga_for_checkout")
    @patch("order.app.acquire_checkout_lock", return_value="lock-token")
    @patch("order.app.release_checkout_lock")
    @patch("order.app.get_order_from_db")
    def test_checkout_returns_200_on_terminal_success(
        self,
        mock_get_order,
        _mock_release,
        _mock_lock,
        mock_ensure_saga,
        mock_publish,
        mock_wait,
    ):
        order_entry = OrderValue(paid=False, items=[("item-1", 2)], user_id="u1", total_cost=10)
        created_saga = SagaValue(
            saga_id="s1",
            order_id="o1",
            user_id="u1",
            items=[],
            total_cost=10,
            state=SAGA_STATE_PENDING,
        )
        terminal_saga = SagaValue(
            saga_id="s1",
            order_id="o1",
            user_id="u1",
            items=[],
            total_cost=10,
            state=SAGA_STATE_COMPLETED,
        )
        mock_get_order.return_value = order_entry
        mock_ensure_saga.return_value = (created_saga, True)
        mock_wait.return_value = terminal_saga

        response = self.client.post("/checkout/o1")

        self.assertEqual(response.status_code, 200)
        mock_publish.assert_called_once_with(created_saga)

    @patch("order.app.wait_for_terminal_saga", return_value=None)
    @patch("order.app.get_saga_from_db")
    @patch("order.app.mark_saga_failed")
    @patch("order.app.publish_checkout_requested")
    @patch("order.app.ensure_saga_for_checkout")
    @patch("order.app.acquire_checkout_lock", return_value="lock-token")
    @patch("order.app.release_checkout_lock")
    @patch("order.app.get_order_from_db")
    def test_checkout_timeout_marks_failed_and_returns_400(
        self,
        mock_get_order,
        _mock_release,
        _mock_lock,
        mock_ensure_saga,
        _mock_publish,
        mock_mark_failed,
        mock_get_saga,
        _mock_wait,
    ):
        order_entry = OrderValue(paid=False, items=[("item-1", 2)], user_id="u1", total_cost=10)
        created_saga = SagaValue(
            saga_id="s2",
            order_id="o2",
            user_id="u1",
            items=[],
            total_cost=10,
            state=SAGA_STATE_PENDING,
        )
        mock_get_order.return_value = order_entry
        mock_ensure_saga.return_value = (created_saga, True)
        mock_get_saga.return_value = created_saga

        response = self.client.post("/checkout/o2")

        self.assertEqual(response.status_code, 400)
        mock_mark_failed.assert_called_once_with(created_saga, "timeout")

    @patch("order.app.wait_for_terminal_saga")
    @patch("order.app.publish_checkout_requested")
    @patch("order.app.ensure_saga_for_checkout")
    @patch("order.app.get_order_active_saga_id", return_value="s-existing")
    @patch("order.app.acquire_checkout_lock", return_value=None)
    @patch("order.app.get_order_from_db")
    def test_checkout_reuses_existing_saga_when_lock_not_acquired(
        self,
        mock_get_order,
        _mock_lock,
        _mock_active_saga,
        mock_ensure_saga,
        mock_publish,
        mock_wait,
    ):
        order_entry = OrderValue(paid=False, items=[("item-1", 2)], user_id="u1", total_cost=10)
        terminal_saga = SagaValue(
            saga_id="s-existing",
            order_id="o1",
            user_id="u1",
            items=[],
            total_cost=10,
            state=SAGA_STATE_COMPLETED,
        )
        mock_get_order.return_value = order_entry
        mock_wait.return_value = terminal_saga

        response = self.client.post("/checkout/o1")

        self.assertEqual(response.status_code, 200)
        mock_ensure_saga.assert_not_called()
        mock_publish.assert_not_called()

    @patch("order.app.get_order_active_saga_id", return_value=None)
    @patch("order.app.acquire_checkout_lock", return_value=None)
    @patch("order.app.get_order_from_db")
    def test_checkout_returns_400_if_lock_not_acquired_and_no_active_saga(
        self,
        mock_get_order,
        _mock_lock,
        _mock_active_saga,
    ):
        order_entry = OrderValue(paid=False, items=[("item-1", 2)], user_id="u1", total_cost=10)
        mock_get_order.return_value = order_entry

        response = self.client.post("/checkout/o1")

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
