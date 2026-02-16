import os
import sys
import time
import unittest
import uuid

import msgspec

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from shared_messaging.codec import encode_message
from shared_messaging.consumer import validate_for_consumer
from shared_messaging.contracts import MessageMetadata, OrderFailedPayload


def build_metadata() -> MessageMetadata:
    message_id = str(uuid.uuid4())
    return MessageMetadata(
        message_id=message_id,
        saga_id=str(uuid.uuid4()),
        order_id="order-7",
        step="consumer-test",
        attempt=1,
        timestamp=int(time.time()),
        correlation_id=message_id,
        causation_id=message_id,
    )


class TestPhase2ConsumerStubValidation(unittest.TestCase):
    def test_valid_message_decision_is_ack(self):
        body = encode_message("OrderFailed", build_metadata(), OrderFailedPayload(reason="timeout"))
        decision = validate_for_consumer(body)

        self.assertEqual(decision.action, "ack")
        self.assertIsNotNone(decision.message)
        assert decision.message is not None
        self.assertEqual(decision.message.message_type, "OrderFailed")

    def test_malformed_message_decision_is_reject(self):
        malformed = {
            "message_type": "OrderFailed",
            "payload": {"reason": "timeout"},
        }
        decision = validate_for_consumer(msgspec.json.encode(malformed))

        self.assertEqual(decision.action, "reject")
        self.assertIsNotNone(decision.reason)
        assert decision.reason is not None
        self.assertIn("Missing required metadata fields", decision.reason)


if __name__ == "__main__":
    unittest.main()
