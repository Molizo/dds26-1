import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "redis")
os.environ.setdefault("REDIS_DB", "0")

from order.models import (
    SAGA_STATE_CHARGING_PAYMENT,
    SAGA_STATE_COMPLETED,
    SAGA_STATE_RELEASING_STOCK,
    SagaValue,
)
from order.workers import order_worker
from shared_messaging.contracts import (
    MessageMetadata,
    PaymentChargedPayload,
    PaymentRejectedPayload,
    StockReleasedPayload,
)


def build_metadata(message_id: str = "m1") -> MessageMetadata:
    return MessageMetadata(
        message_id=message_id,
        saga_id="saga-1",
        order_id="order-1",
        step="phase5-test",
        attempt=1,
        timestamp=1,
        correlation_id="corr-1",
        causation_id="cause-1",
    )


class TestPhase5WorkerEventChaining(unittest.TestCase):
    @patch("order.workers.order_worker._publish_internal_message")
    @patch("order.workers.order_worker._save_saga")
    @patch("order.workers.order_worker._load_saga")
    def test_payment_rejected_moves_to_release_stock(
        self,
        mock_load_saga,
        _mock_save_saga,
        mock_publish,
    ):
        saga = SagaValue(
            saga_id="saga-1",
            order_id="order-1",
            user_id="user-1",
            items=[],
            total_cost=50,
            state=SAGA_STATE_CHARGING_PAYMENT,
        )
        mock_load_saga.return_value = saga
        message = SimpleNamespace(
            message_type="PaymentRejected",
            metadata=build_metadata("message-payment-rejected"),
            payload=PaymentRejectedPayload(reason="insufficient_credit"),
        )

        order_worker._dispatch_message(SimpleNamespace(), message)

        self.assertEqual(saga.state, SAGA_STATE_RELEASING_STOCK)
        self.assertEqual(mock_publish.call_count, 1)
        self.assertEqual(mock_publish.call_args.kwargs["message_type"], "ReleaseStock")
        self.assertEqual(mock_publish.call_args.kwargs["exchange"], "stock.command")

    @patch("order.workers.order_worker._publish_order_terminal_event")
    @patch("order.workers.order_worker._fail_saga")
    @patch("order.workers.order_worker._load_saga")
    def test_stock_released_marks_failed(
        self,
        mock_load_saga,
        mock_fail_saga,
        mock_terminal_event,
    ):
        saga = SagaValue(
            saga_id="saga-1",
            order_id="order-1",
            user_id="user-1",
            items=[],
            total_cost=50,
            state=SAGA_STATE_RELEASING_STOCK,
        )
        mock_load_saga.return_value = saga
        message = SimpleNamespace(
            message_type="StockReleased",
            metadata=build_metadata("message-stock-released"),
            payload=StockReleasedPayload(items=[]),
        )

        order_worker._dispatch_message(SimpleNamespace(), message)

        mock_fail_saga.assert_called_once_with(saga, reason="payment_rejected")
        mock_terminal_event.assert_called_once()

    @patch("order.workers.order_worker._publish_order_terminal_event")
    @patch("order.workers.order_worker._mark_order_completed")
    @patch("order.workers.order_worker._save_saga")
    @patch("order.workers.order_worker._load_saga")
    def test_payment_charged_commits_order(
        self,
        mock_load_saga,
        _mock_save_saga,
        mock_mark_order_completed,
        mock_terminal_event,
    ):
        saga = SagaValue(
            saga_id="saga-1",
            order_id="order-1",
            user_id="user-1",
            items=[],
            total_cost=50,
            state=SAGA_STATE_CHARGING_PAYMENT,
        )
        mock_load_saga.return_value = saga
        message = SimpleNamespace(
            message_type="PaymentCharged",
            metadata=build_metadata("message-payment-charged"),
            payload=PaymentChargedPayload(amount=50),
        )

        order_worker._dispatch_message(SimpleNamespace(), message)

        self.assertEqual(saga.state, SAGA_STATE_COMPLETED)
        mock_mark_order_completed.assert_called_once_with("order-1")
        mock_terminal_event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
