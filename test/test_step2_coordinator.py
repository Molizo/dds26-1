"""Unit tests for Step 2: coordinator protocol logic.

These tests mock the messaging layer and ports to verify protocol behavior
without requiring RabbitMQ or Redis.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call
from typing import Optional

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from common.constants import (
    STATUS_INIT, STATUS_HOLDING, STATUS_HELD, STATUS_COMMITTING,
    STATUS_COMPENSATING, STATUS_COMPLETED, STATUS_ABORTED,
    STATUS_FAILED_NEEDS_RECOVERY, TERMINAL_STATUSES,
    SVC_STOCK, SVC_PAYMENT,
    CMD_HOLD, CMD_RELEASE, CMD_COMMIT,
    PROTOCOL_SAGA, PROTOCOL_2PC,
)
from common.models import ParticipantReply
from common.result import CheckoutResult
from coordinator.models import CheckoutTxValue, make_tx
from coordinator.ports import OrderSnapshot
from coordinator.service import CoordinatorService, _aggregate_items


def _make_snapshot(order_id="order-1", user_id="user-1", total_cost=100,
                   paid=False, items=None):
    if items is None:
        items = [("item-1", 2), ("item-2", 3)]
    return OrderSnapshot(
        order_id=order_id, user_id=user_id, total_cost=total_cost,
        paid=paid, items=items,
    )


def _stock_reply(tx_id, ok=True, error=None, command=CMD_HOLD):
    return ParticipantReply(tx_id=tx_id, service=SVC_STOCK, command=command,
                            ok=ok, error=error)


def _payment_reply(tx_id, ok=True, error=None, command=CMD_HOLD):
    return ParticipantReply(tx_id=tx_id, service=SVC_PAYMENT, command=command,
                            ok=ok, error=error)


class _MockOrderPort:
    def __init__(self, snapshot=None):
        self._snapshot = snapshot or _make_snapshot()
        self.mark_paid_calls = []

    def read_order(self, order_id):
        return self._snapshot

    def mark_paid(self, order_id, tx_id):
        self.mark_paid_calls.append((order_id, tx_id))
        return True


class _MockTxStore:
    def __init__(self):
        self.txs = {}
        self.decisions = {}
        self.fences = {}
        self.guards = {}

    def create_tx(self, tx):
        self.txs[tx.tx_id] = tx

    def update_tx(self, tx):
        self.txs[tx.tx_id] = tx

    def get_tx(self, tx_id):
        return self.txs.get(tx_id)

    def get_non_terminal_txs(self):
        return [tx for tx in self.txs.values() if tx.status not in TERMINAL_STATUSES]

    def set_decision(self, tx_id, decision):
        self.decisions[tx_id] = decision

    def get_decision(self, tx_id):
        return self.decisions.get(tx_id)

    def set_commit_fence(self, order_id, tx_id):
        self.fences[order_id] = tx_id

    def get_commit_fence(self, order_id):
        return self.fences.get(order_id)

    def clear_commit_fence(self, order_id):
        self.fences.pop(order_id, None)

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
        return order_id in self.guards


def _make_coordinator(order_port=None, tx_store=None,
                      hold_replies=None, commit_replies=None, release_replies=None):
    """Create a CoordinatorService with mocked messaging.

    hold_replies: replies returned during hold phase
    commit_replies: replies returned during commit phase
    release_replies: replies returned during release phase
    """
    if order_port is None:
        order_port = _MockOrderPort()
    if tx_store is None:
        tx_store = _MockTxStore()

    coordinator = CoordinatorService(
        rabbitmq_url="amqp://test",
        order_port=order_port,
        tx_store=tx_store,
    )

    # Track which phase we're in by watching published commands
    _call_count = {"holds": 0, "commits": 0, "releases": 0}

    def mock_wait(tx_id, timeout):
        # Determine phase from call sequence
        # Each phase calls register_pending then wait_for_replies
        all_replies = []
        if _call_count["holds"] == 0 and hold_replies is not None:
            all_replies = hold_replies
            _call_count["holds"] = 1
        elif _call_count["commits"] == 0 and commit_replies is not None:
            all_replies = commit_replies
            _call_count["commits"] = 1
        elif _call_count["releases"] == 0 and release_replies is not None:
            all_replies = release_replies
            _call_count["releases"] = 1
        return all_replies

    return coordinator, order_port, tx_store, mock_wait


@patch("coordinator.service.publish_command")
@patch("coordinator.service.get_reply_queue", return_value="test.replies")
class TestSagaProtocol(unittest.TestCase):

    def _run(self, mock_queue, mock_publish, hold_replies=None,
             commit_replies=None, release_replies=None, order_port=None,
             tx_store=None, protocol=PROTOCOL_SAGA):
        coordinator, op, ts, mock_wait = _make_coordinator(
            order_port=order_port, tx_store=tx_store,
            hold_replies=hold_replies, commit_replies=commit_replies,
            release_replies=release_replies,
        )
        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", side_effect=mock_wait), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.execute_checkout("order-1", protocol)
        return result, op, ts

    def test_saga_happy_path(self, mock_queue, mock_publish):
        """Both holds succeed → mark paid → send commits → COMPLETED."""
        hold = [_stock_reply("tx", ok=True), _payment_reply("tx", ok=True)]
        commit = [_stock_reply("tx", ok=True, command=CMD_COMMIT),
                  _payment_reply("tx", ok=True, command=CMD_COMMIT)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, commit_replies=commit)

        self.assertTrue(result.success)
        self.assertEqual(result.status_code, 200)
        # Order was marked paid
        self.assertEqual(len(op.mark_paid_calls), 1)
        # Tx reached COMPLETED
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_COMPLETED)
        self.assertTrue(tx.stock_held)
        self.assertTrue(tx.payment_held)
        self.assertTrue(tx.stock_committed)
        self.assertTrue(tx.payment_committed)
        # Decision was persisted as commit
        self.assertEqual(ts.decisions[tx.tx_id], "commit")

    def test_saga_stock_rejection(self, mock_queue, mock_publish):
        """Stock fails → compensate payment → ABORTED."""
        hold = [_stock_reply("tx", ok=False, error="insufficient_stock"),
                _payment_reply("tx", ok=True)]
        release = [_stock_reply("tx", ok=True, command=CMD_RELEASE),
                   _payment_reply("tx", ok=True, command=CMD_RELEASE)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, release_replies=release)

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)
        self.assertFalse(tx.stock_held)
        self.assertTrue(tx.payment_held)
        # No mark_paid call
        self.assertEqual(len(op.mark_paid_calls), 0)

    def test_saga_payment_rejection(self, mock_queue, mock_publish):
        """Payment fails → compensate stock → ABORTED."""
        hold = [_stock_reply("tx", ok=True),
                _payment_reply("tx", ok=False, error="insufficient_credit")]
        release = [_stock_reply("tx", ok=True, command=CMD_RELEASE),
                   _payment_reply("tx", ok=True, command=CMD_RELEASE)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, release_replies=release)

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)

    def test_saga_both_reject(self, mock_queue, mock_publish):
        """Both fail → compensate both → ABORTED."""
        hold = [_stock_reply("tx", ok=False, error="insufficient_stock"),
                _payment_reply("tx", ok=False, error="insufficient_credit")]
        release = [_stock_reply("tx", ok=True, command=CMD_RELEASE),
                   _payment_reply("tx", ok=True, command=CMD_RELEASE)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, release_replies=release)

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)

    def test_saga_both_timeout(self, mock_queue, mock_publish):
        """Both time out (no replies) → release/refund both → ABORTED."""
        release = [_stock_reply("tx", ok=True, command=CMD_RELEASE),
                   _payment_reply("tx", ok=True, command=CMD_RELEASE)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=[], release_replies=release)

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)

    def test_saga_single_timeout_stock(self, mock_queue, mock_publish):
        """Stock times out, payment succeeds → compensate both."""
        hold = [_payment_reply("tx", ok=True)]  # only payment replies
        release = [_stock_reply("tx", ok=True, command=CMD_RELEASE),
                   _payment_reply("tx", ok=True, command=CMD_RELEASE)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, release_replies=release)

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)

    def test_saga_already_paid(self, mock_queue, mock_publish):
        """Order already paid → return success immediately."""
        order_port = _MockOrderPort(snapshot=_make_snapshot(paid=True))
        coordinator, _, _, _ = _make_coordinator(order_port=order_port)

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies"), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.execute_checkout("order-1", PROTOCOL_SAGA)

        self.assertTrue(result.success)
        self.assertTrue(result.already_paid)

    def test_saga_commit_incomplete_sets_failed_needs_recovery(self, mock_queue, mock_publish):
        """Commit reply times out → FAILED_NEEDS_RECOVERY (order still paid)."""
        hold = [_stock_reply("tx", ok=True), _payment_reply("tx", ok=True)]
        commit = []  # no commit replies

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, commit_replies=commit)

        # Result is still success (order IS paid)
        self.assertTrue(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_FAILED_NEEDS_RECOVERY)
        # FAILED_NEEDS_RECOVERY is NOT terminal
        self.assertNotIn(STATUS_FAILED_NEEDS_RECOVERY, TERMINAL_STATUSES)


@patch("coordinator.service.publish_command")
@patch("coordinator.service.get_reply_queue", return_value="test.replies")
class TestTwoPCProtocol(unittest.TestCase):

    def _run(self, mock_queue, mock_publish, hold_replies=None,
             commit_replies=None, release_replies=None, order_port=None,
             tx_store=None):
        coordinator, op, ts, mock_wait = _make_coordinator(
            order_port=order_port, tx_store=tx_store,
            hold_replies=hold_replies, commit_replies=commit_replies,
            release_replies=release_replies,
        )
        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", side_effect=mock_wait), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.execute_checkout("order-1", PROTOCOL_2PC)
        return result, op, ts

    def test_2pc_happy_path(self, mock_queue, mock_publish):
        """Both prepare → commit decision → commit both → COMPLETED."""
        hold = [_stock_reply("tx", ok=True), _payment_reply("tx", ok=True)]
        commit = [_stock_reply("tx", ok=True, command=CMD_COMMIT),
                  _payment_reply("tx", ok=True, command=CMD_COMMIT)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, commit_replies=commit)

        self.assertTrue(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_COMPLETED)
        # In 2PC, mark_paid happens AFTER commits confirmed
        self.assertEqual(len(op.mark_paid_calls), 1)
        # Decision persisted as commit
        self.assertEqual(ts.decisions[tx.tx_id], "commit")

    def test_2pc_prepare_rejection(self, mock_queue, mock_publish):
        """One prepare fails → abort all → ABORTED."""
        hold = [_stock_reply("tx", ok=True),
                _payment_reply("tx", ok=False, error="insufficient_credit")]
        release = [_stock_reply("tx", ok=True, command=CMD_RELEASE),
                   _payment_reply("tx", ok=True, command=CMD_RELEASE)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, release_replies=release)

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)
        self.assertEqual(ts.decisions[tx.tx_id], "abort")

    def test_2pc_timeout_presumed_abort(self, mock_queue, mock_publish):
        """Both timeout → presumed abort."""
        release = [_stock_reply("tx", ok=True, command=CMD_RELEASE),
                   _payment_reply("tx", ok=True, command=CMD_RELEASE)]

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=[], release_replies=release)

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_ABORTED)

    def test_2pc_decision_before_committing(self, mock_queue, mock_publish):
        """Verify decision marker written before COMMITTING status."""
        hold = [_stock_reply("tx", ok=True), _payment_reply("tx", ok=True)]
        commit = [_stock_reply("tx", ok=True, command=CMD_COMMIT),
                  _payment_reply("tx", ok=True, command=CMD_COMMIT)]

        # Track the order of calls
        call_order = []
        original_tx_store = _MockTxStore()
        orig_set_decision = original_tx_store.set_decision
        orig_update_tx = original_tx_store.update_tx

        def track_set_decision(tx_id, decision):
            call_order.append(("set_decision", decision))
            return orig_set_decision(tx_id, decision)

        def track_update_tx(tx):
            call_order.append(("update_tx", tx.status))
            return orig_update_tx(tx)

        original_tx_store.set_decision = track_set_decision
        original_tx_store.update_tx = track_update_tx

        result, _, ts = self._run(mock_queue, mock_publish,
                                  hold_replies=hold, commit_replies=commit,
                                  tx_store=original_tx_store)

        # Find where commit decision and COMMITTING status appear
        decision_idx = next(i for i, c in enumerate(call_order)
                           if c == ("set_decision", "commit"))
        committing_idx = next(i for i, c in enumerate(call_order)
                              if c == ("update_tx", STATUS_COMMITTING))
        self.assertLess(decision_idx, committing_idx,
                        "Decision must be written before COMMITTING status")

    def test_2pc_mark_paid_before_completed(self, mock_queue, mock_publish):
        """2PC: mark_paid must be called before STATUS_COMPLETED is written.

        Verifies the P1 fix: crash after COMPLETED but before mark_paid would
        leave a terminal tx with an unpaid order. The correct sequence is
        commits → mark_paid → COMPLETED.
        """
        hold = [_stock_reply("tx", ok=True), _payment_reply("tx", ok=True)]
        commit = [_stock_reply("tx", ok=True, command=CMD_COMMIT),
                  _payment_reply("tx", ok=True, command=CMD_COMMIT)]

        call_order = []
        original_tx_store = _MockTxStore()
        original_order_port = _MockOrderPort()

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

        result, _, _ = self._run(mock_queue, mock_publish,
                                 hold_replies=hold, commit_replies=commit,
                                 order_port=original_order_port,
                                 tx_store=original_tx_store)

        self.assertTrue(result.success)

        paid_idx = call_order.index("mark_paid")
        completed_idx = next(
            i for i, c in enumerate(call_order)
            if c == ("update_tx", STATUS_COMPLETED)
        )
        self.assertLess(paid_idx, completed_idx,
                        "mark_paid must be called before STATUS_COMPLETED is persisted")

    def test_2pc_mark_paid_failure_sets_failed_needs_recovery(self, mock_queue, mock_publish):
        """2PC: if mark_paid fails after commits, status becomes FAILED_NEEDS_RECOVERY, not COMPLETED."""
        hold = [_stock_reply("tx", ok=True), _payment_reply("tx", ok=True)]
        commit = [_stock_reply("tx", ok=True, command=CMD_COMMIT),
                  _payment_reply("tx", ok=True, command=CMD_COMMIT)]

        order_port = _MockOrderPort()
        order_port.mark_paid = lambda order_id, tx_id: False  # always fails

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, commit_replies=commit,
                                   order_port=order_port)

        self.assertFalse(result.success)
        tx = list(ts.txs.values())[0]
        self.assertNotEqual(tx.status, STATUS_COMPLETED,
                            "COMPLETED must never be written when mark_paid fails")
        self.assertEqual(tx.status, STATUS_FAILED_NEEDS_RECOVERY)
        self.assertEqual(tx.last_error, "mark_paid_failed")

    def test_2pc_commit_incomplete_order_not_paid(self, mock_queue, mock_publish):
        """2PC: if commits don't confirm, order must NOT be marked paid."""
        hold = [_stock_reply("tx", ok=True), _payment_reply("tx", ok=True)]
        commit = []  # no commit replies — timeout

        result, op, ts = self._run(mock_queue, mock_publish,
                                   hold_replies=hold, commit_replies=commit)

        # Result is failure (order is not paid in 2PC until commits confirmed)
        self.assertFalse(result.success)
        # Order must NOT have been marked paid
        self.assertEqual(len(op.mark_paid_calls), 0,
                         "mark_paid must not be called when 2PC commits are incomplete")
        tx = list(ts.txs.values())[0]
        self.assertEqual(tx.status, STATUS_FAILED_NEEDS_RECOVERY)


class TestAggregateItems(unittest.TestCase):

    def test_merges_duplicates(self):
        items = [("a", 1), ("b", 2), ("a", 3)]
        result = _aggregate_items(items)
        self.assertEqual(result, [("a", 4), ("b", 2)])

    def test_sorted_deterministic(self):
        items = [("z", 1), ("a", 1), ("m", 1)]
        result = _aggregate_items(items)
        self.assertEqual([x[0] for x in result], ["a", "m", "z"])

    def test_empty(self):
        self.assertEqual(_aggregate_items([]), [])


class TestSagaResume(unittest.TestCase):
    """Test resume_transaction for SAGA recovery paths."""

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_committing_returns_checkout_result(self, mock_queue, mock_publish):
        """Recovery: COMMITTING SAGA returns CheckoutResult, not a bare bool."""
        order_port = _MockOrderPort(snapshot=_make_snapshot(paid=True))
        tx_store = _MockTxStore()

        tx = make_tx(
            tx_id="tx-saga-commit", order_id="order-1", user_id="user-1",
            total_cost=100, protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 2)], status=STATUS_COMMITTING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx.decision = "commit"
        tx_store.create_tx(tx)

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)

        commit_replies = [_stock_reply("tx-saga-commit", ok=True, command=CMD_COMMIT),
                          _payment_reply("tx-saga-commit", ok=True, command=CMD_COMMIT)]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=commit_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertIsInstance(result, CheckoutResult)
        self.assertTrue(result.success)
        self.assertEqual(tx_store.txs["tx-saga-commit"].status, STATUS_COMPLETED)

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_both_held_completes_forward(self, mock_queue, mock_publish):
        """Recovery: both held → commit forward."""
        order_port = _MockOrderPort()
        tx_store = _MockTxStore()

        tx = make_tx(
            tx_id="tx-1", order_id="order-1", user_id="user-1",
            total_cost=100, protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 2)], status=STATUS_HOLDING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx_store.create_tx(tx)

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)

        commit_replies = [_stock_reply("tx-1", ok=True, command=CMD_COMMIT),
                          _payment_reply("tx-1", ok=True, command=CMD_COMMIT)]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=commit_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertTrue(result.success)
        self.assertEqual(tx_store.txs["tx-1"].status, STATUS_COMPLETED)

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_partial_hold_compensates(self, mock_queue, mock_publish):
        """Recovery: stock held but payment not → compensate."""
        order_port = _MockOrderPort()
        tx_store = _MockTxStore()

        tx = make_tx(
            tx_id="tx-2", order_id="order-1", user_id="user-1",
            total_cost=100, protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 2)], status=STATUS_HOLDING,
        )
        tx.stock_held = True
        tx.payment_held = False
        tx_store.create_tx(tx)

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)

        release_replies = [_stock_reply("tx-2", ok=True, command=CMD_RELEASE),
                           _payment_reply("tx-2", ok=True, command=CMD_RELEASE)]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=release_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertFalse(result.success)
        self.assertEqual(tx_store.txs["tx-2"].status, STATUS_ABORTED)


class TestTwoPCResume(unittest.TestCase):

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_with_commit_decision(self, mock_queue, mock_publish):
        """Recovery: commit decision exists → re-publish commits."""
        order_port = _MockOrderPort()
        tx_store = _MockTxStore()

        tx = make_tx(
            tx_id="tx-3", order_id="order-1", user_id="user-1",
            total_cost=100, protocol=PROTOCOL_2PC,
            items_snapshot=[("item-1", 2)], status=STATUS_COMMITTING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx.decision = "commit"
        tx_store.create_tx(tx)
        tx_store.set_decision("tx-3", "commit")

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)

        commit_replies = [_stock_reply("tx-3", ok=True, command=CMD_COMMIT),
                          _payment_reply("tx-3", ok=True, command=CMD_COMMIT)]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=commit_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertTrue(result.success)
        self.assertEqual(tx_store.txs["tx-3"].status, STATUS_COMPLETED)
        # Order marked paid after commits
        self.assertEqual(len(order_port.mark_paid_calls), 1)

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_2pc_mark_paid_failure_sets_failed_needs_recovery(self, mock_queue, mock_publish):
        """Recovery 2PC: mark_paid failure after commits → FAILED_NEEDS_RECOVERY, not COMPLETED."""
        order_port = _MockOrderPort(snapshot=_make_snapshot(paid=False))
        order_port.mark_paid = lambda order_id, tx_id: False  # always fails
        tx_store = _MockTxStore()

        tx = make_tx(
            tx_id="tx-rmp", order_id="order-1", user_id="user-1",
            total_cost=100, protocol=PROTOCOL_2PC,
            items_snapshot=[("item-1", 2)], status=STATUS_COMMITTING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx.decision = "commit"
        tx_store.create_tx(tx)
        tx_store.set_decision("tx-rmp", "commit")

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)

        commit_replies = [_stock_reply("tx-rmp", ok=True, command=CMD_COMMIT),
                          _payment_reply("tx-rmp", ok=True, command=CMD_COMMIT)]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=commit_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertFalse(result.success)
        self.assertNotEqual(tx_store.txs["tx-rmp"].status, STATUS_COMPLETED,
                            "COMPLETED must never be written when mark_paid fails")
        self.assertEqual(tx_store.txs["tx-rmp"].status, STATUS_FAILED_NEEDS_RECOVERY)
        self.assertEqual(tx_store.txs["tx-rmp"].last_error, "mark_paid_failed")

    @patch("coordinator.service.publish_command")
    @patch("coordinator.service.get_reply_queue", return_value="test.replies")
    def test_resume_no_decision_presumed_abort(self, mock_queue, mock_publish):
        """Recovery: no commit decision → presumed abort."""
        order_port = _MockOrderPort(snapshot=_make_snapshot(paid=False))
        tx_store = _MockTxStore()

        tx = make_tx(
            tx_id="tx-4", order_id="order-1", user_id="user-1",
            total_cost=100, protocol=PROTOCOL_2PC,
            items_snapshot=[("item-1", 2)], status=STATUS_HOLDING,
        )
        tx.stock_held = True
        tx.payment_held = True
        tx_store.create_tx(tx)

        coordinator = CoordinatorService("amqp://test", order_port, tx_store)

        release_replies = [_stock_reply("tx-4", ok=True, command=CMD_RELEASE),
                           _payment_reply("tx-4", ok=True, command=CMD_RELEASE)]

        with patch("coordinator.service.register_pending"), \
             patch("coordinator.service.wait_for_replies", return_value=release_replies), \
             patch("coordinator.service.cancel_pending"):
            result = coordinator.resume_transaction(tx)

        self.assertFalse(result.success)
        self.assertEqual(tx_store.txs["tx-4"].status, STATUS_ABORTED)


class TestReplyCorrelationFiltering(unittest.TestCase):
    """Tests for the expected_command filter in coordinator/messaging.py.

    This covers the bug where a late/stale hold reply from a previous phase
    (that arrives during the commit phase) could signal the wrong Event and
    cause the coordinator to proceed with incomplete commit confirmations.
    """

    def setUp(self):
        # Reset module-level correlation state between tests
        import coordinator.messaging as m
        with m._correlation_lock:
            m._correlation_map.clear()
        # Patch the reply queue so _on_reply calls don't need a real consumer
        import coordinator.messaging as m
        m._reply_queue = "test.replies"

    def tearDown(self):
        import coordinator.messaging as m
        with m._correlation_lock:
            m._correlation_map.clear()

    def test_out_of_phase_reply_is_ignored(self):
        """A hold reply arriving during the commit phase must not signal the event."""
        from coordinator.messaging import register_pending, wait_for_replies, _on_reply, _correlation_map

        # Register for commit phase (waiting only on stock)
        register_pending("tx-x", expected_command=CMD_COMMIT,
                         expected_services=frozenset({SVC_STOCK}))

        # Simulate a stale hold reply arriving on the queue
        stale_hold = _stock_reply("tx-x", ok=True, command=CMD_HOLD)
        from common.models import encode_reply
        import pika

        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 1
        mock_props = MagicMock()

        _on_reply(mock_channel, mock_method, mock_props, encode_reply(stale_hold))

        # The event should NOT be set — stale reply was filtered
        import coordinator.messaging as m
        with m._correlation_lock:
            entry = m._correlation_map.get("tx-x")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.event.is_set(), "Stale hold reply must not signal commit phase event")
        self.assertEqual(len(entry.replies), 0)

    def test_duplicate_service_reply_ignored(self):
        """Two replies from the same service must not double-satisfy the wait."""
        import coordinator.messaging as m
        from coordinator.messaging import register_pending, _on_reply
        from common.models import encode_reply

        register_pending("tx-dup", expected_command=CMD_COMMIT,
                         expected_services=frozenset({SVC_STOCK, SVC_PAYMENT}))

        first = _stock_reply("tx-dup", ok=True, command=CMD_COMMIT)
        second = _stock_reply("tx-dup", ok=True, command=CMD_COMMIT)  # duplicate
        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 1

        _on_reply(mock_channel, mock_method, MagicMock(), encode_reply(first))
        _on_reply(mock_channel, mock_method, MagicMock(), encode_reply(second))

        with m._correlation_lock:
            entry = m._correlation_map.get("tx-dup")
        self.assertIsNotNone(entry)
        # Event must NOT be set — only 1 unique service replied, need 2
        self.assertFalse(entry.event.is_set(),
                         "Duplicate service reply must not satisfy expected=2 wait")
        self.assertEqual(len(entry.replies), 1, "Only one reply should be stored")

    def test_correct_phase_reply_signals_event(self):
        """A commit reply arriving during the commit phase signals the event."""
        import coordinator.messaging as m
        from coordinator.messaging import register_pending, _on_reply
        from common.models import encode_reply

        register_pending("tx-y", expected_command=CMD_COMMIT,
                         expected_services=frozenset({SVC_STOCK}))

        commit_reply = _stock_reply("tx-y", ok=True, command=CMD_COMMIT)
        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 1

        _on_reply(mock_channel, mock_method, MagicMock(), encode_reply(commit_reply))

        with m._correlation_lock:
            entry = m._correlation_map.get("tx-y")
        # Event is set and reply was recorded
        self.assertIsNotNone(entry)
        self.assertTrue(entry.event.is_set())
        self.assertEqual(len(entry.replies), 1)

    def test_wrong_service_reply_ignored(self):
        """A reply from an unexpected service must not satisfy the wait."""
        import coordinator.messaging as m
        from coordinator.messaging import register_pending, _on_reply
        from common.models import encode_reply

        # Register waiting only on payment
        register_pending("tx-ws", expected_command=CMD_COMMIT,
                         expected_services=frozenset({SVC_PAYMENT}))

        # Deliver a stock commit reply (wrong service for this wait)
        stock_commit = _stock_reply("tx-ws", ok=True, command=CMD_COMMIT)
        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 1

        _on_reply(mock_channel, mock_method, MagicMock(), encode_reply(stock_commit))

        with m._correlation_lock:
            entry = m._correlation_map.get("tx-ws")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.event.is_set(),
                         "Stock reply must not satisfy a wait registered for payment only")
        self.assertEqual(len(entry.replies), 0)

    def test_failed_needs_recovery_guard_not_cleared(self):
        """Guard must NOT be cleared when tx ends in FAILED_NEEDS_RECOVERY."""
        from coordinator.models import make_tx

        tx_store = _MockTxStore()
        tx = make_tx(
            tx_id="tx-fnr", order_id="order-fnr", user_id="user-1",
            total_cost=100, protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 1)], status=STATUS_FAILED_NEEDS_RECOVERY,
        )
        tx_store.create_tx(tx)
        tx_store.guards["order-fnr"] = "tx-fnr"

        # Simulate the guard-cleanup logic from order/app.py
        if tx.status in TERMINAL_STATUSES:
            tx_store.clear_active_tx_guard("order-fnr")

        # Guard must still be present
        self.assertIn("order-fnr", tx_store.guards,
                      "Guard must NOT be cleared for FAILED_NEEDS_RECOVERY")

    def test_terminal_status_guard_cleared(self):
        """Guard IS cleared when tx reaches COMPLETED or ABORTED."""
        from coordinator.models import make_tx

        for terminal_status in TERMINAL_STATUSES:
            tx_store = _MockTxStore()
            tx = make_tx(
                tx_id="tx-done", order_id="order-done", user_id="user-1",
                total_cost=100, protocol=PROTOCOL_SAGA,
                items_snapshot=[("item-1", 1)], status=terminal_status,
            )
            tx_store.create_tx(tx)
            tx_store.guards["order-done"] = "tx-done"

            # Simulate guard-cleanup logic
            if tx.status in TERMINAL_STATUSES:
                tx_store.clear_active_tx_guard("order-done")

            self.assertNotIn("order-done", tx_store.guards,
                             f"Guard must be cleared for status={terminal_status}")


if __name__ == '__main__':
    unittest.main()
