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

from order.workers import dlq_replay_worker


class FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}
        self.expiry: dict[str, int] = {}

    def get(self, key):
        value = self.store.get(key)
        if value is None:
            return None
        return str(value).encode("utf-8")

    def incr(self, key):
        value = self.store.get(key, 0) + 1
        self.store[key] = value
        return value

    def expire(self, key, ttl_sec):
        self.expiry[key] = ttl_sec


class FakeChannel:
    def __init__(self):
        self.calls = []
        self.raise_on_publish = False

    def basic_publish(self, **kwargs):
        if self.raise_on_publish:
            raise RuntimeError("publish failed")
        self.calls.append(("publish", kwargs))

    def basic_ack(self, **kwargs):
        self.calls.append(("ack", kwargs))

    def basic_nack(self, **kwargs):
        self.calls.append(("nack", kwargs))


class TestPhase6DlqReplayWorker(unittest.TestCase):
    def test_source_queue_falls_back_to_dlq_mapping(self):
        source = dlq_replay_worker._source_queue_from_headers({}, "stock.command.dlq")
        self.assertEqual(source, "stock.command.q")

    def test_derive_original_message_id_uses_body_hash_for_invalid_message(self):
        original_id = dlq_replay_worker._derive_original_message_id(b"not-json", SimpleNamespace(message_id=None))
        self.assertTrue(original_id.startswith("body-hash:"))

    @patch("order.workers.dlq_replay_worker._throttle")
    def test_replay_below_attempt_cap_republishes_and_acks(self, _mock_throttle):
        channel = FakeChannel()
        redis_client = FakeRedis()
        method = SimpleNamespace(delivery_tag=11)
        properties = SimpleNamespace(
            headers={"x-dds-source-queue": "stock.command.q"},
            app_id=None,
            content_encoding=None,
            content_type="application/json",
            correlation_id="corr-1",
            message_id="msg-1",
            type="ReserveStock",
        )

        dlq_replay_worker._handle_one_dlq_message(
            channel,
            redis_client,
            dlq_queue_name="stock.command.dlq",
            method=method,
            properties=properties,
            body=b'{"message_type":"ReserveStock"}',
            max_attempts=3,
            attempt_ttl_sec=86400,
            rate_limit_state={"last_publish_at": 0.0},
            replay_rate_per_sec=20,
        )

        self.assertEqual(channel.calls[0][0], "publish")
        self.assertEqual(channel.calls[0][1]["exchange"], "stock.command")
        self.assertEqual(channel.calls[1][0], "ack")
        key = dlq_replay_worker.dlq_replay_attempt_key("msg-1")
        self.assertEqual(redis_client.store[key], 1)

    @patch("order.workers.dlq_replay_worker._throttle")
    def test_replay_above_attempt_cap_moves_to_parking(self, _mock_throttle):
        channel = FakeChannel()
        redis_client = FakeRedis()
        key = dlq_replay_worker.dlq_replay_attempt_key("msg-2")
        redis_client.store[key] = 3
        method = SimpleNamespace(delivery_tag=22)
        properties = SimpleNamespace(
            headers={"x-dds-source-queue": "order.command.q"},
            app_id=None,
            content_encoding=None,
            content_type="application/json",
            correlation_id="corr-2",
            message_id="msg-2",
            type="OrderFailed",
        )

        dlq_replay_worker._handle_one_dlq_message(
            channel,
            redis_client,
            dlq_queue_name="order.command.dlq",
            method=method,
            properties=properties,
            body=b'{"message_type":"OrderFailed"}',
            max_attempts=3,
            attempt_ttl_sec=86400,
            rate_limit_state={"last_publish_at": 0.0},
            replay_rate_per_sec=20,
        )

        self.assertEqual(channel.calls[0][0], "publish")
        self.assertEqual(channel.calls[0][1]["exchange"], "dlq.parking")
        self.assertEqual(channel.calls[1][0], "ack")

    @patch("order.workers.dlq_replay_worker._throttle")
    def test_invalid_source_queue_is_parked(self, _mock_throttle):
        channel = FakeChannel()
        redis_client = FakeRedis()
        method = SimpleNamespace(delivery_tag=33)
        properties = SimpleNamespace(
            headers={"x-dds-source-queue": "unknown.q"},
            app_id=None,
            content_encoding=None,
            content_type="application/json",
            correlation_id="corr-3",
            message_id="msg-3",
            type="unknown",
        )

        dlq_replay_worker._handle_one_dlq_message(
            channel,
            redis_client,
            dlq_queue_name="stock.command.dlq",
            method=method,
            properties=properties,
            body=b'{"message_type":"ReserveStock"}',
            max_attempts=3,
            attempt_ttl_sec=86400,
            rate_limit_state={"last_publish_at": 0.0},
            replay_rate_per_sec=20,
        )

        self.assertEqual(channel.calls[0][1]["exchange"], "dlq.parking")
        self.assertEqual(channel.calls[1][0], "ack")

    @patch("order.workers.dlq_replay_worker._throttle")
    def test_publish_failure_nacks_for_retry(self, _mock_throttle):
        channel = FakeChannel()
        channel.raise_on_publish = True
        redis_client = FakeRedis()
        method = SimpleNamespace(delivery_tag=44)
        properties = SimpleNamespace(
            headers={"x-dds-source-queue": "payment.command.q"},
            app_id=None,
            content_encoding=None,
            content_type="application/json",
            correlation_id="corr-4",
            message_id="msg-4",
            type="ChargePayment",
        )

        with self.assertRaises(RuntimeError):
            dlq_replay_worker._handle_one_dlq_message(
                channel,
                redis_client,
                dlq_queue_name="payment.command.dlq",
                method=method,
                properties=properties,
                body=b'{"message_type":"ChargePayment"}',
                max_attempts=3,
                attempt_ttl_sec=86400,
                rate_limit_state={"last_publish_at": 0.0},
                replay_rate_per_sec=20,
            )


if __name__ == "__main__":
    unittest.main()
