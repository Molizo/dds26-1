import os
import sys
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "redis")
os.environ.setdefault("REDIS_DB", "0")

from stock.workers import stock_worker


class FakeChannel:
    def __init__(self):
        self.is_open = True
        self.prefetch_count = None
        self.consumed_queue = None
        self.auto_ack = None
        self.closed = False

    def basic_qos(self, *, prefetch_count):
        self.prefetch_count = prefetch_count

    def basic_consume(self, *, queue, on_message_callback, auto_ack):
        del on_message_callback
        self.consumed_queue = queue
        self.auto_ack = auto_ack

    def start_consuming(self):
        raise KeyboardInterrupt()

    def close(self):
        self.closed = True
        self.is_open = False


class FakeConnection:
    def __init__(self):
        self._channel = FakeChannel()
        self.is_open = True
        self.closed = False

    def channel(self):
        return self._channel

    def close(self):
        self.closed = True
        self.is_open = False


class TestPhase4WorkerReconnect(unittest.TestCase):
    @patch("stock.workers.stock_worker.time.sleep")
    @patch("stock.workers.stock_worker.pika.BlockingConnection")
    def test_run_consumer_retries_connection_then_recovers(self, mock_connection, mock_sleep):
        os.environ["WORKER_PREFETCH_COUNT"] = "3"
        os.environ["WORKER_MAX_RETRIES"] = "5"
        os.environ["WORKER_RECONNECT_BACKOFF_MS"] = "250"
        os.environ["RABBITMQ_QUEUE"] = "stock.command.q"

        good_connection = FakeConnection()
        mock_connection.side_effect = [RuntimeError("broker down"), good_connection]

        stock_worker.run_consumer()

        self.assertEqual(mock_sleep.call_count, 1)
        self.assertEqual(mock_sleep.call_args.args[0], 0.25)
        self.assertEqual(good_connection._channel.prefetch_count, 3)
        self.assertEqual(good_connection._channel.consumed_queue, "stock.command.q")
        self.assertEqual(good_connection._channel.auto_ack, False)
        self.assertTrue(good_connection._channel.closed)
        self.assertTrue(good_connection.closed)


if __name__ == "__main__":
    unittest.main()
