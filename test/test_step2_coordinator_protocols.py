"""Unit tests for Step 2 protocol execution paths."""
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
)
from coordinator.ports import OrderPortUnavailable
from coordinator.service import _aggregate_items
from helpers.coordinator_doubles import (
    MockOrderPort,
    MockTxStore,
    make_coordinator,
    make_snapshot,
    payment_reply,
    stock_reply,
)


@patch("coordinator.service.publish_command")
@patch("coordinator.service.get_reply_queue", return_value="test.replies")
class TestSagaProtocol(unittest.TestCase):
    def _run(
        self,
        mock_queue,
        mock_publish,
        *,
        hold_replies=None,
        commit_replies=None,
        release_replies=None,
        order_port=None,
        tx_store=None,
    ):
        coordinator, op, ts, mock_wait = make_coordinator(
            order_port=order_port,
            tx_store=tx_store,
            hold_replies=hold_replies,
            commit_replies=commit_replies,
            release_replies=release_replies,
        )
        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", side_effect=mock_wait), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.execute_checkout(make_snapshot(), PROTOCOL_SAGA)
        return result, op, ts

    def test_saga_happy_path(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]
        commit = [
            stock_reply("tx", ok=True, command=CMD_COMMIT),
            payment_reply("tx", ok=True, command=CMD_COMMIT),
        ]

        result, op, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            commit_replies=commit,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(op.mark_paid_calls), 1)
        self.assertEqual(op.read_order_calls, [])
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_COMPLETED)
        self.assertTrue(tx.stock_held)
        self.assertTrue(tx.payment_held)
        self.assertTrue(tx.stock_committed)
        self.assertTrue(tx.payment_committed)
        self.assertEqual(ts.decisions[tx.tx_id], "commit")

    def test_saga_stock_rejection(self, mock_queue, mock_publish):
        hold = [
            stock_reply("tx", ok=False, error="insufficient_stock"),
            payment_reply("tx", ok=True),
        ]
        release = [
            stock_reply("tx", ok=True, command=CMD_RELEASE),
            payment_reply("tx", ok=True, command=CMD_RELEASE),
        ]

        result, op, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            release_replies=release,
        )

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)
        self.assertFalse(tx.stock_held)
        self.assertTrue(tx.payment_held)
        self.assertEqual(len(op.mark_paid_calls), 0)

    def test_saga_payment_rejection(self, mock_queue, mock_publish):
        hold = [
            stock_reply("tx", ok=True),
            payment_reply("tx", ok=False, error="insufficient_credit"),
        ]
        release = [
            stock_reply("tx", ok=True, command=CMD_RELEASE),
            payment_reply("tx", ok=True, command=CMD_RELEASE),
        ]

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            release_replies=release,
        )

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)

    def test_saga_both_reject(self, mock_queue, mock_publish):
        hold = [
            stock_reply("tx", ok=False, error="insufficient_stock"),
            payment_reply("tx", ok=False, error="insufficient_credit"),
        ]
        release = [
            stock_reply("tx", ok=True, command=CMD_RELEASE),
            payment_reply("tx", ok=True, command=CMD_RELEASE),
        ]

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            release_replies=release,
        )

        self.assertFalse(result.success)
        self.assertEqual(list(ts.txs.values())[0].status, STATUS_ABORTED)

    def test_saga_both_timeout(self, mock_queue, mock_publish):
        release = [
            stock_reply("tx", ok=True, command=CMD_RELEASE),
            payment_reply("tx", ok=True, command=CMD_RELEASE),
        ]

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=[],
            release_replies=release,
        )

        self.assertFalse(result.success)
        self.assertEqual(list(ts.txs.values())[0].status, STATUS_ABORTED)

    def test_saga_single_timeout_stock(self, mock_queue, mock_publish):
        hold = [payment_reply("tx", ok=True)]
        release = [
            stock_reply("tx", ok=True, command=CMD_RELEASE),
            payment_reply("tx", ok=True, command=CMD_RELEASE),
        ]

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            release_replies=release,
        )

        self.assertFalse(result.success)
        self.assertEqual(list(ts.txs.values())[0].status, STATUS_ABORTED)

    def test_saga_already_paid_snapshot_short_circuit(self, mock_queue, mock_publish):
        coordinator, _, _, _ = make_coordinator(order_port=MockOrderPort(snapshot=make_snapshot(paid=True)))
        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies"), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.execute_checkout(make_snapshot(paid=True), PROTOCOL_SAGA)

        self.assertTrue(result.success)
        self.assertTrue(result.already_paid)

    def test_saga_commit_incomplete_sets_failed_needs_recovery(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            commit_replies=[],
        )

        self.assertTrue(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_FAILED_NEEDS_RECOVERY)

    def test_saga_mark_paid_indeterminate_sets_failed_needs_recovery(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]
        order_port = MockOrderPort()

        def _boom(order_id, tx_id):
            raise OrderPortUnavailable("mark_paid_timeout")

        order_port.mark_paid = _boom

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            order_port=order_port,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "mark_paid_indeterminate")
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_FAILED_NEEDS_RECOVERY)
        self.assertEqual(tx.last_error, "mark_paid_indeterminate")


@patch("coordinator.service.publish_command")
@patch("coordinator.service.get_reply_queue", return_value="test.replies")
class TestTwoPCProtocol(unittest.TestCase):
    def _run(
        self,
        mock_queue,
        mock_publish,
        *,
        hold_replies=None,
        commit_replies=None,
        release_replies=None,
        order_port=None,
        tx_store=None,
    ):
        coordinator, op, ts, mock_wait = make_coordinator(
            order_port=order_port,
            tx_store=tx_store,
            hold_replies=hold_replies,
            commit_replies=commit_replies,
            release_replies=release_replies,
        )
        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", side_effect=mock_wait), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.execute_checkout(make_snapshot(), PROTOCOL_2PC)
        return result, op, ts

    def test_2pc_happy_path(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]
        commit = [
            stock_reply("tx", ok=True, command=CMD_COMMIT),
            payment_reply("tx", ok=True, command=CMD_COMMIT),
        ]

        result, op, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            commit_replies=commit,
        )

        self.assertTrue(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_COMPLETED)
        self.assertEqual(len(op.mark_paid_calls), 1)
        self.assertEqual(op.read_order_calls, [])
        self.assertEqual(ts.decisions[tx.tx_id], "commit")

    def test_2pc_prepare_rejection(self, mock_queue, mock_publish):
        hold = [
            stock_reply("tx", ok=True),
            payment_reply("tx", ok=False, error="insufficient_credit"),
        ]
        release = [
            stock_reply("tx", ok=True, command=CMD_RELEASE),
            payment_reply("tx", ok=True, command=CMD_RELEASE),
        ]

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            release_replies=release,
        )

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)
        self.assertEqual(ts.decisions[tx.tx_id], "abort")

    def test_2pc_timeout_presumed_abort(self, mock_queue, mock_publish):
        release = [
            stock_reply("tx", ok=True, command=CMD_RELEASE),
            payment_reply("tx", ok=True, command=CMD_RELEASE),
        ]

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=[],
            release_replies=release,
        )

        self.assertFalse(result.success)
        self.assertEqual(list(ts.txs.values())[0].status, STATUS_ABORTED)

    def test_2pc_decision_before_committing(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]
        commit = [
            stock_reply("tx", ok=True, command=CMD_COMMIT),
            payment_reply("tx", ok=True, command=CMD_COMMIT),
        ]

        call_order = []
        original_tx_store = MockTxStore()
        orig_persist = original_tx_store.set_decision_fence_and_update_tx

        def track_persist(tx_id, decision, order_id, tx):
            call_order.append(("persist_commit_state", decision, tx.status, order_id))
            return orig_persist(tx_id, decision, order_id, tx)

        original_tx_store.set_decision_fence_and_update_tx = track_persist

        result, _, _ = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            commit_replies=commit,
            tx_store=original_tx_store,
        )

        self.assertTrue(result.success)
        self.assertIn(
            ("persist_commit_state", "commit", STATUS_COMMITTING, "order-1"),
            call_order,
        )

    def test_2pc_mark_paid_before_completed(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]
        commit = [
            stock_reply("tx", ok=True, command=CMD_COMMIT),
            payment_reply("tx", ok=True, command=CMD_COMMIT),
        ]

        call_order = []
        original_tx_store = MockTxStore()
        original_order_port = MockOrderPort()

        orig_mark_paid = original_order_port.mark_paid
        orig_update_tx = original_tx_store.update_tx

        def track_mark_paid(order_id, tx_id):
            call_order.append("mark_paid")
            return orig_mark_paid(order_id, tx_id)

        def track_update_tx(tx):
            call_order.append(("update_tx", tx.status))
            return orig_update_tx(tx)

        original_order_port.mark_paid = track_mark_paid
        original_tx_store.update_tx = track_update_tx

        result, _, _ = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            commit_replies=commit,
            order_port=original_order_port,
            tx_store=original_tx_store,
        )

        self.assertTrue(result.success)
        paid_idx = call_order.index("mark_paid")
        completed_idx = next(
            i for i, c in enumerate(call_order)
            if c == ("update_tx", STATUS_COMPLETED)
        )
        self.assertLess(paid_idx, completed_idx)

    def test_2pc_mark_paid_failure_sets_failed_needs_recovery(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]
        commit = [
            stock_reply("tx", ok=True, command=CMD_COMMIT),
            payment_reply("tx", ok=True, command=CMD_COMMIT),
        ]

        order_port = MockOrderPort()
        order_port.mark_paid = lambda order_id, tx_id: False

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            commit_replies=commit,
            order_port=order_port,
        )

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_FAILED_NEEDS_RECOVERY)
        self.assertEqual(tx.last_error, "mark_paid_failed")

    def test_2pc_mark_paid_indeterminate_sets_failed_needs_recovery(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]
        commit = [
            stock_reply("tx", ok=True, command=CMD_COMMIT),
            payment_reply("tx", ok=True, command=CMD_COMMIT),
        ]

        order_port = MockOrderPort()

        def _boom(order_id, tx_id):
            raise OrderPortUnavailable("mark_paid_timeout")

        order_port.mark_paid = _boom

        result, _, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            commit_replies=commit,
            order_port=order_port,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "mark_paid_indeterminate")
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_FAILED_NEEDS_RECOVERY)
        self.assertEqual(tx.last_error, "mark_paid_indeterminate")

    def test_2pc_commit_incomplete_order_not_paid(self, mock_queue, mock_publish):
        hold = [stock_reply("tx", ok=True), payment_reply("tx", ok=True)]

        result, op, ts = self._run(
            mock_queue,
            mock_publish,
            hold_replies=hold,
            commit_replies=[],
        )

        self.assertFalse(result.success)
        self.assertEqual(len(op.mark_paid_calls), 0)
        self.assertEqual(list(ts.txs.values())[0].status, STATUS_FAILED_NEEDS_RECOVERY)


class TestAggregateItems(unittest.TestCase):
    def test_merges_duplicates(self):
        items = [("a", 1), ("b", 2), ("a", 3)]
        self.assertEqual(_aggregate_items(items), [("a", 4), ("b", 2)])

    def test_sorted_deterministic(self):
        items = [("z", 1), ("a", 1), ("m", 1)]
        self.assertEqual([x[0] for x in _aggregate_items(items)], ["a", "m", "z"])

    def test_empty(self):
        self.assertEqual(_aggregate_items([]), [])


if __name__ == "__main__":
    unittest.main()
