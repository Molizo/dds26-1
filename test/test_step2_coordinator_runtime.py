"""Unit tests for reply correlation filters and orchestrator runtime boundaries."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
from common.models import decode_internal_reply, decode_reply, encode_internal_reply, encode_reply
from common.result import CheckoutResult
from common.worker_support import handle_internal_rpc_delivery, handle_participant_delivery
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

    def test_correct_phase_reply_records_before_ack(self):
        import coordinator.messaging as messaging
        from coordinator.messaging import _on_reply, register_pending

        register_pending(
            "tx-ack",
            expected_command=CMD_COMMIT,
            expected_services=frozenset({SVC_STOCK}),
        )

        def assert_recorded_before_ack(*, delivery_tag):
            self.assertEqual(delivery_tag, 1)
            with messaging._correlation_lock:
                entry = messaging._correlation_map.get("tx-ack")
                self.assertIsNotNone(entry)
                self.assertTrue(entry.event.is_set())
                self.assertEqual(len(entry.replies), 1)

        mock_channel = MagicMock()
        mock_channel.basic_ack.side_effect = assert_recorded_before_ack
        mock_method = MagicMock()
        mock_method.delivery_tag = 1

        _on_reply(
            mock_channel,
            mock_method,
            MagicMock(),
            encode_reply(stock_reply("tx-ack", ok=True, command=CMD_COMMIT)),
        )

        mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)

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
    def test_execute_checkout_acquires_guard_before_order_read(self):
        call_order = []

        class _TrackingTxStore(MockTxStore):
            def acquire_active_tx_guard(self, order_id, tx_id, ttl):
                call_order.append("guard")
                return super().acquire_active_tx_guard(order_id, tx_id, ttl)

        class _TrackingOrderPort(MockOrderPort):
            def read_order(self, order_id):
                call_order.append("read")
                return super().read_order(order_id)

        order_port = _TrackingOrderPort(snapshot=make_snapshot())
        tx_store = _TrackingTxStore()
        coordinator = MagicMock()
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
        self.assertEqual(call_order[:2], ["guard", "read"])

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
        self.assertIsNone(tx_store.get_active_tx_guard("order-404"))
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
        self.assertIsNone(tx_store.get_active_tx_guard("order-1"))
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

    def test_execute_checkout_clears_guard_when_order_read_fails(self):
        class _FailingOrderPort:
            def __init__(self):
                self.read_order_calls = []

            def read_order(self, order_id):
                self.read_order_calls.append(order_id)
                raise RuntimeError("order down")

        order_port = _FailingOrderPort()
        tx_store = MockTxStore()
        coordinator = MagicMock()
        logger = MagicMock()
        runtime = OrchestratorRuntime(
            db=MagicMock(),
            rabbitmq_url="amqp://test",
            checkout_protocol=PROTOCOL_SAGA,
            logger=logger,
            order_port=order_port,
            tx_store_adapter=tx_store,
            coordinator=coordinator,
        )

        result = runtime.execute_checkout("order-err", "tx-err")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "order_read_failed")
        self.assertEqual(order_port.read_order_calls, ["order-err"])
        self.assertIsNone(tx_store.get_active_tx_guard("order-err"))
        coordinator.execute_checkout.assert_not_called()
        logger.exception.assert_called_once()

    def test_acquire_mutation_guard_normalizes_transient_busy_to_retryable_reason(self):
        class _BusyTxStore(MockTxStore):
            def __init__(self):
                super().__init__()
                self._attempts = 0

            def acquire_mutation_guard(self, order_id, lease_id, ttl):
                self._attempts += 1
                return False

            def get_mutation_guard(self, order_id):
                return None

        runtime = OrchestratorRuntime(
            db=MagicMock(),
            rabbitmq_url="amqp://test",
            checkout_protocol=PROTOCOL_SAGA,
            logger=MagicMock(),
            order_port=MockOrderPort(snapshot=make_snapshot()),
            tx_store_adapter=_BusyTxStore(),
            coordinator=MagicMock(),
        )

        acquired, reason, status_code = runtime.acquire_mutation_guard("order-1", "lease-1")

        self.assertFalse(acquired)
        self.assertEqual(reason, "mutation_in_progress")
        self.assertEqual(status_code, 409)


class TestRpcReplyConsumer(unittest.TestCase):
    def setUp(self):
        import common.rpc as rpc
        with rpc._reply_lock:
            rpc._pending_replies.clear()

    def tearDown(self):
        import common.rpc as rpc
        with rpc._reply_lock:
            rpc._pending_replies.clear()

    def test_on_reply_records_body_before_ack(self):
        import common.rpc as rpc

        entry = rpc._PendingReply()
        with rpc._reply_lock:
            rpc._pending_replies["req-1"] = entry

        def assert_recorded_before_ack(*, delivery_tag):
            self.assertEqual(delivery_tag, 1)
            self.assertEqual(entry.body, b"reply-body")
            self.assertTrue(entry.event.is_set())

        mock_channel = MagicMock()
        mock_channel.basic_ack.side_effect = assert_recorded_before_ack
        mock_method = MagicMock()
        mock_method.delivery_tag = 1
        properties = SimpleNamespace(correlation_id="req-1")

        rpc._on_reply(mock_channel, mock_method, properties, b"reply-body")

        mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)


class TestReplyConsumerSetup(unittest.TestCase):
    def test_reply_consumer_declares_non_exclusive_queue_that_survives_reconnects(self):
        import common.amqp_consumers as consumers

        channel = MagicMock()
        connection = MagicMock()
        connection.channel.return_value = channel
        channel.start_consuming.side_effect = RuntimeError("stop")
        logger = MagicMock()

        with patch("common.amqp_consumers.pika.BlockingConnection", return_value=connection), \
             patch("common.amqp_consumers.time.sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                consumers._run_exclusive_reply_consumer(
                    "amqp://test",
                    "rpc.replies.test",
                    10,
                    MagicMock(),
                    logger,
                )

        channel.queue_declare.assert_called_once_with(
            queue="rpc.replies.test",
            exclusive=False,
            auto_delete=False,
            arguments={"x-expires": consumers.REPLY_QUEUE_EXPIRES_MS},
        )


class TestWorkerSupport(unittest.TestCase):
    def test_handle_internal_rpc_delivery_nacks_when_publish_fails(self):
        channel = MagicMock()
        method = SimpleNamespace(delivery_tag=7)
        properties = SimpleNamespace(reply_to="rpc.replies", correlation_id="req-1")
        cmd = SimpleNamespace(request_id="req-1")
        reply = object()

        with patch("common.worker_support.publish_message", side_effect=RuntimeError("broker down")):
            handle_internal_rpc_delivery(
                channel=channel,
                method=method,
                properties=properties,
                body=b"cmd",
                rabbitmq_url="amqp://test",
                service_name="orchestrator",
                decode_command=lambda body: cmd,
                dispatch=lambda decoded: reply,
                encode_reply=lambda encoded_reply: b"reply",
                logger=MagicMock(),
            )

        channel.basic_ack.assert_not_called()
        channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=True)

    def test_handle_internal_rpc_delivery_publishes_before_ack(self):
        channel = MagicMock()
        method = SimpleNamespace(delivery_tag=8)
        properties = SimpleNamespace(reply_to="rpc.replies", correlation_id="req-2")
        events = []

        def publish_side_effect(*args, **kwargs):
            events.append("publish")
            channel.basic_ack.assert_not_called()

        def ack_side_effect(*, delivery_tag):
            self.assertEqual(delivery_tag, 8)
            events.append("ack")

        channel.basic_ack.side_effect = ack_side_effect

        with patch("common.worker_support.publish_message", side_effect=publish_side_effect):
            handle_internal_rpc_delivery(
                channel=channel,
                method=method,
                properties=properties,
                body=b"cmd",
                rabbitmq_url="amqp://test",
                service_name="orchestrator",
                decode_command=lambda body: SimpleNamespace(request_id="req-2"),
                dispatch=lambda decoded: object(),
                encode_reply=lambda encoded_reply: b"reply",
                logger=MagicMock(),
            )

        self.assertEqual(events, ["publish", "ack"])
        channel.basic_nack.assert_not_called()

    def test_handle_internal_rpc_delivery_replies_when_dispatch_fails(self):
        channel = MagicMock()
        method = SimpleNamespace(delivery_tag=10)
        properties = SimpleNamespace(reply_to="rpc.replies", correlation_id="req-3")
        published = {}

        def publish_side_effect(_url, routing_key, body, *, correlation_id=None):
            published["routing_key"] = routing_key
            published["correlation_id"] = correlation_id
            published["reply"] = decode_internal_reply(body)

        with patch("common.worker_support.publish_message", side_effect=publish_side_effect):
            handle_internal_rpc_delivery(
                channel=channel,
                method=method,
                properties=properties,
                body=b"cmd",
                rabbitmq_url="amqp://test",
                service_name="orchestrator",
                decode_command=lambda body: SimpleNamespace(request_id="req-3", command="checkout"),
                dispatch=lambda decoded: (_ for _ in ()).throw(RuntimeError("boom")),
                encode_reply=lambda reply: encode_internal_reply(reply),
                logger=MagicMock(),
            )

        self.assertEqual(published["routing_key"], "rpc.replies")
        self.assertEqual(published["correlation_id"], "req-3")
        self.assertFalse(published["reply"].ok)
        self.assertEqual(published["reply"].error, "internal_error")
        self.assertEqual(published["reply"].status_code, 400)
        channel.basic_ack.assert_called_once_with(delivery_tag=10)
        channel.basic_nack.assert_not_called()

    def test_handle_participant_delivery_nacks_when_publish_fails(self):
        channel = MagicMock()
        method = SimpleNamespace(delivery_tag=9)
        cmd = SimpleNamespace(tx_id="tx-1", command="hold", reply_to="coordinator.replies")

        with patch("common.worker_support.publish_reply", side_effect=RuntimeError("broker down")):
            handle_participant_delivery(
                channel=channel,
                method=method,
                body=b"cmd",
                rabbitmq_url="amqp://test",
                service_name="stock",
                decode_command=lambda body: cmd,
                dispatch=lambda decoded: object(),
                encode_reply=lambda encoded_reply: b"reply",
                logger=MagicMock(),
            )

        channel.basic_ack.assert_not_called()
        channel.basic_nack.assert_called_once_with(delivery_tag=9, requeue=True)

    def test_handle_participant_delivery_replies_when_dispatch_fails(self):
        channel = MagicMock()
        method = SimpleNamespace(delivery_tag=11)
        published = {}

        def publish_side_effect(_url, reply_to, body):
            published["reply_to"] = reply_to
            published["reply"] = decode_reply(body)

        with patch("common.worker_support.publish_reply", side_effect=publish_side_effect):
            handle_participant_delivery(
                channel=channel,
                method=method,
                body=b"cmd",
                rabbitmq_url="amqp://test",
                service_name="stock",
                decode_command=lambda body: SimpleNamespace(
                    tx_id="tx-2",
                    command="hold",
                    reply_to="coordinator.replies",
                ),
                dispatch=lambda decoded: (_ for _ in ()).throw(RuntimeError("boom")),
                encode_reply=lambda reply: encode_reply(reply),
                logger=MagicMock(),
            )

        self.assertEqual(published["reply_to"], "coordinator.replies")
        self.assertEqual(published["reply"].tx_id, "tx-2")
        self.assertEqual(published["reply"].service, "stock")
        self.assertEqual(published["reply"].command, "hold")
        self.assertFalse(published["reply"].ok)
        self.assertEqual(published["reply"].error, "internal_error")
        channel.basic_ack.assert_called_once_with(delivery_tag=11)
        channel.basic_nack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
