"""Unit tests for Step 2 recovery and resume paths."""
import os
import sys
import unittest
from unittest.mock import patch

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from common.constants import (
    CMD_COMMIT,
    CMD_RELEASE,
    PROTOCOL_2PC,
    PROTOCOL_SAGA,
    STATUS_ABORTED,
    STATUS_COMMITTING,
    STATUS_COMPLETED,
    STATUS_FAILED_NEEDS_RECOVERY,
    STATUS_HOLDING,
)
from common.result import CheckoutResult
from coordinator.models import make_tx
from helpers.coordinator_doubles import (
    MockOrderPort,
    MockTxStore,
    make_snapshot,
    payment_reply,
    stock_reply,
)
from coordinator.service import CoordinatorService


class TestSagaResume(unittest.TestCase):
    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_committing_returns_checkout_result(self, mock_queue, mock_publish):
        order_port = MockOrderPort(snapshot=make_snapshot(paid=True))
        tx_store = MockTxStore()

        tx = make_tx(
            tx_id="tx-saga-commit",
            order_id="order-1",
            user_id="user-1",
            total_cost=100,
            protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 2)],
            status=STATUS_COMMITTING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx.decision = "commit"
        tx_store.create_tx(tx)

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)
        commit_replies = [
            stock_reply("tx-saga-commit", ok=True, command=CMD_COMMIT),
            payment_reply("tx-saga-commit", ok=True, command=CMD_COMMIT),
        ]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=commit_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertIsInstance(result, CheckoutResult)
        self.assertTrue(result.success)
        self.assertEqual(tx_store.txs["tx-saga-commit"].status, STATUS_COMPLETED)
        self.assertEqual(order_port.read_order_calls, ["order-1"])

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_both_held_completes_forward(self, mock_queue, mock_publish):
        order_port = MockOrderPort()
        tx_store = MockTxStore()

        tx = make_tx(
            tx_id="tx-1",
            order_id="order-1",
            user_id="user-1",
            total_cost=100,
            protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 2)],
            status=STATUS_HOLDING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx_store.create_tx(tx)

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)
        commit_replies = [
            stock_reply("tx-1", ok=True, command=CMD_COMMIT),
            payment_reply("tx-1", ok=True, command=CMD_COMMIT),
        ]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=commit_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertTrue(result.success)
        self.assertEqual(tx_store.txs["tx-1"].status, STATUS_COMPLETED)
        self.assertEqual(order_port.read_order_calls, ["order-1"])

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_partial_hold_compensates(self, mock_queue, mock_publish):
        order_port = MockOrderPort()
        tx_store = MockTxStore()

        tx = make_tx(
            tx_id="tx-2",
            order_id="order-1",
            user_id="user-1",
            total_cost=100,
            protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 2)],
            status=STATUS_HOLDING,
        )
        tx.stock_held = True
        tx.payment_held = False
        tx_store.create_tx(tx)

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)
        release_replies = [
            stock_reply("tx-2", ok=True, command=CMD_RELEASE),
            payment_reply("tx-2", ok=True, command=CMD_RELEASE),
        ]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=release_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertFalse(result.success)
        self.assertEqual(tx_store.txs["tx-2"].status, STATUS_ABORTED)
        self.assertEqual(order_port.read_order_calls, ["order-1"])


class TestTwoPCResume(unittest.TestCase):
    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_with_commit_decision(self, mock_queue, mock_publish):
        order_port = MockOrderPort()
        tx_store = MockTxStore()

        tx = make_tx(
            tx_id="tx-3",
            order_id="order-1",
            user_id="user-1",
            total_cost=100,
            protocol=PROTOCOL_2PC,
            items_snapshot=[("item-1", 2)],
            status=STATUS_COMMITTING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx.decision = "commit"
        tx_store.create_tx(tx)
        tx_store.set_decision("tx-3", "commit")

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)
        commit_replies = [
            stock_reply("tx-3", ok=True, command=CMD_COMMIT),
            payment_reply("tx-3", ok=True, command=CMD_COMMIT),
        ]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=commit_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertTrue(result.success)
        self.assertEqual(tx_store.txs["tx-3"].status, STATUS_COMPLETED)
        self.assertEqual(len(order_port.mark_paid_calls), 1)
        self.assertEqual(order_port.read_order_calls, ["order-1"])

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_2pc_mark_paid_failure_sets_failed_needs_recovery(self, mock_queue, mock_publish):
        order_port = MockOrderPort(snapshot=make_snapshot(paid=False))
        order_port.mark_paid = lambda order_id, tx_id: False
        tx_store = MockTxStore()

        tx = make_tx(
            tx_id="tx-rmp",
            order_id="order-1",
            user_id="user-1",
            total_cost=100,
            protocol=PROTOCOL_2PC,
            items_snapshot=[("item-1", 2)],
            status=STATUS_COMMITTING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx.decision = "commit"
        tx_store.create_tx(tx)
        tx_store.set_decision("tx-rmp", "commit")

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)
        commit_replies = [
            stock_reply("tx-rmp", ok=True, command=CMD_COMMIT),
            payment_reply("tx-rmp", ok=True, command=CMD_COMMIT),
        ]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=commit_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertFalse(result.success)
        self.assertEqual(tx_store.txs["tx-rmp"].status, STATUS_FAILED_NEEDS_RECOVERY)
        self.assertEqual(tx_store.txs["tx-rmp"].last_error, "mark_paid_failed")
        self.assertEqual(order_port.read_order_calls, ["order-1"])

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_no_decision_presumed_abort(self, mock_queue, mock_publish):
        order_port = MockOrderPort(snapshot=make_snapshot(paid=False))
        tx_store = MockTxStore()

        tx = make_tx(
            tx_id="tx-4",
            order_id="order-1",
            user_id="user-1",
            total_cost=100,
            protocol=PROTOCOL_2PC,
            items_snapshot=[("item-1", 2)],
            status=STATUS_HOLDING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx_store.create_tx(tx)

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)
        release_replies = [
            stock_reply("tx-4", ok=True, command=CMD_RELEASE),
            payment_reply("tx-4", ok=True, command=CMD_RELEASE),
        ]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=release_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertFalse(result.success)
        self.assertEqual(tx_store.txs["tx-4"].status, STATUS_ABORTED)
        self.assertEqual(order_port.read_order_calls, ["order-1"])


if __name__ == "__main__":
    unittest.main()
