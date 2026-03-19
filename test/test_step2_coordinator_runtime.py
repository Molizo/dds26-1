"""Unit tests for reply correlation filters and orchestrator runtime boundaries."""
import os
import sys
import unittest
from unittest.mock import MagicMock

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_orchestrator_dir = os.path.join(_repo_root, "orchestrator")
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
if _orchestrator_dir not in sys.path:
    sys.path.insert(0, _orchestrator_dir)

from common.constants import (
    CMD_COMMIT,
    CMD_HOLD,
    PROTOCOL_SAGA,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED_NEEDS_RECOVERY,
    SVC_PAYMENT,
    SVC_STOCK,
    TERMINAL_STATUSES,
)
from common.models import encode_reply
from common.result import CheckoutResult
from coordinator.models import make_tx
from helpers.coordinator_doubles import MockOrderPort, MockTxStore, make_snapshot, stock_reply
from runtime_service import OrchestratorRuntime


class TestReplyCorrelationFiltering(unittest.TestCase):
    def setUp(self):
        import coordinator.messaging as messaging
        with messaging._correlation_lock:
            messaging._correlation_map.clear()
        messaging._reply_queue = "test.replies"

    def tearDown(self):
        import coordinator.messaging as messaging
        with messaging._correlation_lock:
            messaging._correlation_map.clear()

    def test_out_of_phase_reply_is_ignored(self):
        import coordinator.messaging as messaging
        from coordinator.messaging import _on_reply, register_pending

        register_pending(
            "tx-x",
            expected_command=CMD_COMMIT,
            expected_services=frozenset({SVC_STOCK}),
        )

        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 1
        _on_reply(mock_channel, mock_method, MagicMock(), encode_reply(stock_reply("tx-x", ok=True, command=CMD_HOLD)))

        with messaging._correlation_lock:
            entry = messaging._correlation_map.get("tx-x")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.event.is_set())
        self.assertEqual(len(entry.replies), 0)

    def test_duplicate_service_reply_ignored(self):
        import coordinator.messaging as messaging
        from coordinator.messaging import _on_reply, register_pending

        register_pending(
            "tx-dup",
            expected_command=CMD_COMMIT,
            expected_services=frozenset({SVC_STOCK, SVC_PAYMENT}),
        )

        first = stock_reply("tx-dup", ok=True, command=CMD_COMMIT)
        second = stock_reply("tx-dup", ok=True, command=CMD_COMMIT)
        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 1

        _on_reply(mock_channel, mock_method, MagicMock(), encode_reply(first))
        _on_reply(mock_channel, mock_method, MagicMock(), encode_reply(second))

        with messaging._correlation_lock:
            entry = messaging._correlation_map.get("tx-dup")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.event.is_set())
        self.assertEqual(len(entry.replies), 1)

    def test_correct_phase_reply_signals_event(self):
        import coordinator.messaging as messaging
        from coordinator.messaging import _on_reply, register_pending

        register_pending(
            "tx-y",
            expected_command=CMD_COMMIT,
            expected_services=frozenset({SVC_STOCK}),
        )

        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 1
        _on_reply(
            mock_channel,
            mock_method,
            MagicMock(),
            encode_reply(stock_reply("tx-y", ok=True, command=CMD_COMMIT)),
        )

        with messaging._correlation_lock:
            entry = messaging._correlation_map.get("tx-y")
        self.assertIsNotNone(entry)
        self.assertTrue(entry.event.is_set())
        self.assertEqual(len(entry.replies), 1)

    def test_wrong_service_reply_ignored(self):
        import coordinator.messaging as messaging
        from coordinator.messaging import _on_reply, register_pending

        register_pending(
            "tx-ws",
            expected_command=CMD_COMMIT,
            expected_services=frozenset({SVC_PAYMENT}),
        )

        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = 1
        _on_reply(
            mock_channel,
            mock_method,
            MagicMock(),
            encode_reply(stock_reply("tx-ws", ok=True, command=CMD_COMMIT)),
        )

        with messaging._correlation_lock:
            entry = messaging._correlation_map.get("tx-ws")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.event.is_set())
        self.assertEqual(len(entry.replies), 0)

    def test_failed_needs_recovery_guard_not_cleared(self):
        tx_store = MockTxStore()
        tx = make_tx(
            tx_id="tx-fnr",
            order_id="order-fnr",
            user_id="user-1",
            total_cost=100,
            protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 1)],
            status=STATUS_FAILED_NEEDS_RECOVERY,
        )
        tx_store.create_tx(tx)
        tx_store.guards["order-fnr"] = "tx-fnr"

        if tx.status in TERMINAL_STATUSES:
            tx_store.clear_active_tx_guard("order-fnr")

        self.assertIn("order-fnr", tx_store.guards)

    def test_terminal_status_guard_cleared(self):
        for terminal_status in TERMINAL_STATUSES:
            tx_store = MockTxStore()
            tx = make_tx(
                tx_id="tx-done",
                order_id="order-done",
                user_id="user-1",
                total_cost=100,
                protocol=PROTOCOL_SAGA,
                items_snapshot=[("item-1", 1)],
                status=terminal_status,
            )
            tx_store.create_tx(tx)
            tx_store.guards["order-done"] = "tx-done"

            if tx.status in TERMINAL_STATUSES:
                tx_store.clear_active_tx_guard("order-done")

            self.assertNotIn("order-done", tx_store.guards)


class TestOrchestratorRuntime(unittest.TestCase):
    def test_execute_checkout_reads_order_once_and_passes_snapshot(self):
        order_port = MockOrderPort(snapshot=make_snapshot())
        tx_store = MockTxStore()
        coordinator = MagicMock(return_value=CheckoutResult.ok())
        coordinator.execute_checkout.return_value = CheckoutResult.ok()
        runtime = OrchestratorRuntime(
            db=MagicMock(),
            rabbitmq_url="amqp://test",
            checkout_protocol=PROTOCOL_SAGA,
            logger=MagicMock(),
            order_port=order_port,
            tx_store_adapter=tx_store,
            coordinator=coordinator,
        )

        result = runtime.execute_checkout("order-1", "tx-1")

        self.assertTrue(result.success)
        self.assertEqual(order_port.read_order_calls, ["order-1"])
        coordinator.execute_checkout.assert_called_once_with(order_port._snapshot, PROTOCOL_SAGA, "tx-1")

    def test_execute_checkout_short_circuits_missing_order(self):
        order_port = MockOrderPort(snapshot=None)
        tx_store = MockTxStore()
        coordinator = MagicMock()
        runtime = OrchestratorRuntime(
            db=MagicMock(),
            rabbitmq_url="amqp://test",
            checkout_protocol=PROTOCOL_SAGA,
            logger=MagicMock(),
            order_port=order_port,
            tx_store_adapter=tx_store,
            coordinator=coordinator,
        )

        result = runtime.execute_checkout("order-404", "tx-1")

        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(order_port.read_order_calls, ["order-404"])
        coordinator.execute_checkout.assert_not_called()

    def test_execute_checkout_short_circuits_paid_order(self):
        order_port = MockOrderPort(snapshot=make_snapshot(paid=True))
        tx_store = MockTxStore()
        coordinator = MagicMock()
        runtime = OrchestratorRuntime(
            db=MagicMock(),
            rabbitmq_url="amqp://test",
            checkout_protocol=PROTOCOL_SAGA,
            logger=MagicMock(),
            order_port=order_port,
            tx_store_adapter=tx_store,
            coordinator=coordinator,
        )

        result = runtime.execute_checkout("order-1", "tx-1")

        self.assertTrue(result.success)
        self.assertTrue(result.already_paid)
        self.assertEqual(order_port.read_order_calls, ["order-1"])
        coordinator.execute_checkout.assert_not_called()

    def test_execute_checkout_existing_tx_reuses_result_without_read(self):
        tx_store = MockTxStore()
        tx = make_tx(
            tx_id="tx-existing",
            order_id="order-1",
            user_id="user-1",
            total_cost=100,
            protocol=PROTOCOL_SAGA,
            items_snapshot=[("item-1", 1)],
            status=STATUS_COMPLETED,
        )
        tx_store.create_tx(tx)
        order_port = MockOrderPort(snapshot=make_snapshot())
        coordinator = MagicMock()
        runtime = OrchestratorRuntime(
            db=MagicMock(),
            rabbitmq_url="amqp://test",
            checkout_protocol=PROTOCOL_SAGA,
            logger=MagicMock(),
            order_port=order_port,
            tx_store_adapter=tx_store,
            coordinator=coordinator,
        )

        result = runtime.execute_checkout("order-1", "tx-existing")

        self.assertTrue(result.success)
        self.assertEqual(order_port.read_order_calls, [])
        coordinator.execute_checkout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
