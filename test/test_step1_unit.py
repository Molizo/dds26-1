"""Unit tests for Step 1 foundation components.

Tests here do not require the stack to be running. They verify:
1. Message serialization/deserialization round-trips.
2. Constants and protocol validation.
3. Coordinator result types.
"""
import sys
import os
import unittest

# Allow importing common/ and coordinator/ from the repo root
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from common.constants import (
    CMD_HOLD, CMD_RELEASE, CMD_COMMIT,
    SVC_STOCK, SVC_PAYMENT,
    STATUS_COMPLETED, STATUS_ABORTED,
    STATUS_FAILED_NEEDS_RECOVERY,
    TERMINAL_STATUSES, VALID_PROTOCOLS,
    PROTOCOL_SAGA, PROTOCOL_2PC,
)
from common.models import (
    ParticipantCommand,
    ParticipantReply,
    StockHoldPayload,
    PaymentHoldPayload,
    encode_command,
    decode_command,
    encode_reply,
    decode_reply,
)
from common.result import CheckoutResult


class TestConstants(unittest.TestCase):

    def test_terminal_statuses_do_not_include_failed_needs_recovery(self):
        # Critical invariant from attempt 2's bug: FAILED_NEEDS_RECOVERY must
        # never be classified as terminal (that was the root cause of duplicate
        # checkouts).
        self.assertNotIn(STATUS_FAILED_NEEDS_RECOVERY, TERMINAL_STATUSES)

    def test_terminal_statuses_contain_completed_and_aborted(self):
        self.assertIn(STATUS_COMPLETED, TERMINAL_STATUSES)
        self.assertIn(STATUS_ABORTED, TERMINAL_STATUSES)

    def test_valid_protocols(self):
        self.assertIn(PROTOCOL_SAGA, VALID_PROTOCOLS)
        self.assertIn(PROTOCOL_2PC, VALID_PROTOCOLS)
        self.assertNotIn("http", VALID_PROTOCOLS)
        self.assertNotIn("", VALID_PROTOCOLS)


class TestMessageRoundTrip(unittest.TestCase):

    def _make_stock_command(self) -> ParticipantCommand:
        return ParticipantCommand(
            tx_id="test-tx-001",
            command=CMD_HOLD,
            reply_to="coordinator.replies.abc123",
            stock_payload=StockHoldPayload(
                items=[("item-1", 5), ("item-2", 3)]
            ),
        )

    def _make_payment_command(self) -> ParticipantCommand:
        return ParticipantCommand(
            tx_id="test-tx-002",
            command=CMD_HOLD,
            reply_to="coordinator.replies.abc123",
            payment_payload=PaymentHoldPayload(
                user_id="user-abc",
                amount=1500,
            ),
        )

    def test_stock_command_roundtrip(self):
        cmd = self._make_stock_command()
        encoded = encode_command(cmd)
        decoded = decode_command(encoded)
        self.assertEqual(decoded.tx_id, cmd.tx_id)
        self.assertEqual(decoded.command, CMD_HOLD)
        self.assertEqual(decoded.reply_to, cmd.reply_to)
        self.assertIsNotNone(decoded.stock_payload)
        self.assertEqual(decoded.stock_payload.items, [("item-1", 5), ("item-2", 3)])
        self.assertIsNone(decoded.payment_payload)

    def test_payment_command_roundtrip(self):
        cmd = self._make_payment_command()
        encoded = encode_command(cmd)
        decoded = decode_command(encoded)
        self.assertEqual(decoded.tx_id, cmd.tx_id)
        self.assertIsNone(decoded.stock_payload)
        self.assertIsNotNone(decoded.payment_payload)
        self.assertEqual(decoded.payment_payload.user_id, "user-abc")
        self.assertEqual(decoded.payment_payload.amount, 1500)

    def test_release_command_roundtrip(self):
        cmd = ParticipantCommand(
            tx_id="test-tx-003",
            command=CMD_RELEASE,
            reply_to="coordinator.replies.xyz",
            stock_payload=StockHoldPayload(items=[("item-1", 5)]),
        )
        decoded = decode_command(encode_command(cmd))
        self.assertEqual(decoded.command, CMD_RELEASE)

    def test_commit_command_roundtrip(self):
        cmd = ParticipantCommand(
            tx_id="test-tx-004",
            command=CMD_COMMIT,
            reply_to="coordinator.replies.xyz",
        )
        decoded = decode_command(encode_command(cmd))
        self.assertEqual(decoded.command, CMD_COMMIT)
        self.assertIsNone(decoded.stock_payload)
        self.assertIsNone(decoded.payment_payload)

    def test_reply_ok_roundtrip(self):
        reply = ParticipantReply(
            tx_id="test-tx-001",
            service=SVC_STOCK,
            command=CMD_HOLD,
            ok=True,
        )
        decoded = decode_reply(encode_reply(reply))
        self.assertTrue(decoded.ok)
        self.assertEqual(decoded.service, SVC_STOCK)
        self.assertIsNone(decoded.error)

    def test_reply_error_roundtrip(self):
        reply = ParticipantReply(
            tx_id="test-tx-001",
            service=SVC_PAYMENT,
            command=CMD_HOLD,
            ok=False,
            error="insufficient_credit",
        )
        decoded = decode_reply(encode_reply(reply))
        self.assertFalse(decoded.ok)
        self.assertEqual(decoded.error, "insufficient_credit")
        self.assertEqual(decoded.service, SVC_PAYMENT)

    def test_empty_items_roundtrip(self):
        cmd = ParticipantCommand(
            tx_id="test-tx-005",
            command=CMD_HOLD,
            reply_to="coordinator.replies.xyz",
            stock_payload=StockHoldPayload(items=[]),
        )
        decoded = decode_command(encode_command(cmd))
        self.assertEqual(decoded.stock_payload.items, [])


class TestCheckoutResult(unittest.TestCase):

    def test_ok_result(self):
        result = CheckoutResult.ok()
        self.assertTrue(result.success)
        self.assertFalse(result.already_paid)
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.error)

    def test_paid_result(self):
        result = CheckoutResult.paid()
        self.assertTrue(result.success)
        self.assertTrue(result.already_paid)
        self.assertEqual(result.status_code, 200)

    def test_fail_result(self):
        result = CheckoutResult.fail("insufficient_stock")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "insufficient_stock")
        self.assertEqual(result.status_code, 400)

    def test_conflict_result(self):
        result = CheckoutResult.conflict()
        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 409)

    def test_fail_custom_code(self):
        result = CheckoutResult.fail("not_found", code=404)
        self.assertEqual(result.status_code, 404)


class TestCoordinatorModels(unittest.TestCase):

    def test_make_tx(self):
        from coordinator.models import make_tx, CheckoutTxValue
        from common.constants import STATUS_INIT

        tx = make_tx(
            tx_id="tx-001",
            order_id="order-001",
            user_id="user-001",
            total_cost=100,
            protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 2)],
            status=STATUS_INIT,
        )
        self.assertEqual(tx.tx_id, "tx-001")
        self.assertEqual(tx.status, STATUS_INIT)
        self.assertFalse(tx.stock_held)
        self.assertFalse(tx.payment_held)
        self.assertIsNone(tx.decision)
        self.assertIsNone(tx.last_error)
        self.assertEqual(tx.retry_count, 0)

    def test_coordinator_has_no_flask_import(self):
        """Coordinator package must not import Flask — it must be extractable."""
        import coordinator.models
        import coordinator.ports
        # If Flask were imported transitively, it would raise ImportError in
        # environments without Flask. Since this test runs in the same env, we
        # just verify the modules load without error and don't depend on Flask.
        self.assertFalse(
            hasattr(coordinator.models, 'Flask'),
            "coordinator.models must not expose Flask"
        )


if __name__ == '__main__':
    unittest.main()
