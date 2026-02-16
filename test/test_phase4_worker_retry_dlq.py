import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from shared_messaging.consumer import (
    RETRY_DESTINATION_DLQ,
    RETRY_DESTINATION_RETRY,
    decide_retry_destination,
    retry_count_from_headers,
)

os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "redis")
os.environ.setdefault("REDIS_DB", "0")

from stock.workers import stock_worker


class FakeChannel:
    def __init__(self):
        self.calls = []
        self.raise_on_publish = False

    def basic_ack(self, **kwargs):
        self.calls.append(("ack", kwargs))

    def basic_nack(self, **kwargs):
        self.calls.append(("nack", kwargs))

    def basic_reject(self, **kwargs):
        self.calls.append(("reject", kwargs))

    def basic_publish(self, **kwargs):
        if self.raise_on_publish:
            raise RuntimeError("publish failed")
        self.calls.append(("publish", kwargs))


class TestPhase4RetryDlqRouting(unittest.TestCase):
    def test_retry_count_from_headers_uses_matching_queue_only(self):
        headers = {
            "x-death": [
                {"queue": "stock.command.q", "count": 2},
                {"queue": "stock.command.retry.q", "count": 3},
                {"queue": b"stock.command.q", "count": b"4"},
            ]
        }
        self.assertEqual(retry_count_from_headers(headers, "stock.command.q"), 6)

    def test_decide_retry_destination(self):
        headers = {"x-death": [{"queue": "stock.command.q", "count": 2}]}
        self.assertEqual(
            decide_retry_destination(headers, queue_name="stock.command.q", max_retries=5),
            RETRY_DESTINATION_RETRY,
        )
        self.assertEqual(
            decide_retry_destination(headers, queue_name="stock.command.q", max_retries=2),
            RETRY_DESTINATION_DLQ,
        )

    @patch("stock.workers.stock_worker.process_delivery", return_value="retry")
    def test_retry_below_cap_rejects_to_retry_path(self, _mocked_process):
        channel = FakeChannel()
        method = SimpleNamespace(delivery_tag=11)
        properties = SimpleNamespace(headers={})

        stock_worker.on_message(
            channel,
            method,
            properties,
            b"{}",
            queue_name="stock.command.q",
            max_retries=5,
            dlx_exchange="stock.dlx",
            dlx_routing_key="stock.dlq",
        )

        self.assertEqual(channel.calls[0][0], "reject")
        self.assertEqual(channel.calls[0][1]["requeue"], False)

    @patch("stock.workers.stock_worker.process_delivery", return_value="retry")
    def test_retry_at_cap_routes_to_dlq_and_acks(self, _mocked_process):
        channel = FakeChannel()
        method = SimpleNamespace(delivery_tag=22)
        properties = SimpleNamespace(
            headers={"x-death": [{"queue": "stock.command.q", "count": 5}]},
            app_id=None,
            content_encoding=None,
            content_type=None,
            correlation_id=None,
            message_id=None,
            type=None,
        )

        stock_worker.on_message(
            channel,
            method,
            properties,
            b'{"message_type":"ReserveStock"}',
            queue_name="stock.command.q",
            max_retries=5,
            dlx_exchange="stock.dlx",
            dlx_routing_key="stock.dlq",
        )

        self.assertEqual(channel.calls[0][0], "publish")
        self.assertEqual(channel.calls[0][1]["exchange"], "stock.dlx")
        self.assertEqual(channel.calls[1][0], "ack")

    @patch("stock.workers.stock_worker.process_delivery", return_value="reject")
    def test_reject_routes_directly_to_dlq_and_acks(self, _mocked_process):
        channel = FakeChannel()
        method = SimpleNamespace(delivery_tag=33)
        properties = SimpleNamespace(
            headers={},
            app_id=None,
            content_encoding=None,
            content_type=None,
            correlation_id=None,
            message_id=None,
            type=None,
        )

        stock_worker.on_message(
            channel,
            method,
            properties,
            b"not-valid",
            queue_name="stock.command.q",
            max_retries=5,
            dlx_exchange="stock.dlx",
            dlx_routing_key="stock.dlq",
        )

        self.assertEqual(channel.calls[0][0], "publish")
        self.assertEqual(channel.calls[1][0], "ack")

    @patch("stock.workers.stock_worker.process_delivery", return_value="reject")
    def test_dlq_publish_failure_requeues_message(self, _mocked_process):
        channel = FakeChannel()
        channel.raise_on_publish = True
        method = SimpleNamespace(delivery_tag=44)
        properties = SimpleNamespace(
            headers={},
            app_id=None,
            content_encoding=None,
            content_type=None,
            correlation_id=None,
            message_id=None,
            type=None,
        )

        stock_worker.on_message(
            channel,
            method,
            properties,
            b"broken",
            queue_name="stock.command.q",
            max_retries=5,
            dlx_exchange="stock.dlx",
            dlx_routing_key="stock.dlq",
        )

        self.assertEqual(channel.calls[-1][0], "nack")
        self.assertEqual(channel.calls[-1][1]["requeue"], True)


if __name__ == "__main__":
    unittest.main()
