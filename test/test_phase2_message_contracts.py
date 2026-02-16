import os
import sys
import time
import unittest
import uuid

import msgspec

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from shared_messaging.codec import decode_message, encode_message
from shared_messaging.contracts import (
    ChargePaymentPayload,
    CheckoutRequestedPayload,
    ItemQuantity,
    MessageMetadata,
    OrderCommittedPayload,
    OrderFailedPayload,
    PaymentChargedPayload,
    PaymentRejectedPayload,
    ReleaseStockPayload,
    ReserveStockPayload,
    StockRejectedPayload,
    StockReleasedPayload,
    StockReservedPayload,
)
from shared_messaging.errors import MessageValidationError, UnsupportedMessageTypeError


def build_metadata() -> MessageMetadata:
    message_id = str(uuid.uuid4())
    return MessageMetadata(
        message_id=message_id,
        saga_id=str(uuid.uuid4()),
        order_id="order-123",
        step="phase2-test",
        attempt=1,
        timestamp=int(time.time()),
        correlation_id=message_id,
        causation_id=message_id,
    )


def build_payloads() -> dict[str, object]:
    items = [ItemQuantity(item_id="item-1", quantity=2)]
    return {
        "CheckoutRequested": CheckoutRequestedPayload(
            user_id="user-1",
            total_cost=20,
            items=items,
        ),
        "ReserveStock": ReserveStockPayload(items=items),
        "StockReserved": StockReservedPayload(items=items),
        "StockRejected": StockRejectedPayload(reason="out_of_stock"),
        "StockReleased": StockReleasedPayload(items=items),
        "ChargePayment": ChargePaymentPayload(user_id="user-1", amount=20),
        "PaymentCharged": PaymentChargedPayload(amount=20),
        "PaymentRejected": PaymentRejectedPayload(reason="insufficient_credit"),
        "ReleaseStock": ReleaseStockPayload(items=items),
        "OrderCommitted": OrderCommittedPayload(),
        "OrderFailed": OrderFailedPayload(reason="timeout"),
    }


class TestPhase2MessageContracts(unittest.TestCase):
    def test_round_trip_for_every_supported_type(self):
        metadata = build_metadata()
        for message_type, payload in build_payloads().items():
            encoded = encode_message(message_type, metadata, payload)
            decoded = decode_message(encoded)

            self.assertEqual(decoded.message_type, message_type)
            self.assertEqual(decoded.metadata.message_id, metadata.message_id)
            self.assertEqual(msgspec.to_builtins(decoded.payload), msgspec.to_builtins(payload))

    def test_missing_metadata_field_is_rejected(self):
        payload = {"reason": "timeout"}
        malformed = {
            "message_type": "OrderFailed",
            "message_id": "m1",
            "order_id": "o1",
            "step": "x",
            "attempt": 1,
            "timestamp": 1,
            "correlation_id": "m1",
            "causation_id": "m1",
            "payload": payload,
        }

        with self.assertRaises(MessageValidationError):
            decode_message(msgspec.json.encode(malformed))

    def test_unknown_message_type_is_rejected(self):
        metadata = build_metadata()
        bad_payload = {"value": "x"}
        body = {
            "message_type": "UnknownType",
            **msgspec.to_builtins(metadata),
            "payload": bad_payload,
        }

        with self.assertRaises(UnsupportedMessageTypeError):
            decode_message(msgspec.json.encode(body))


if __name__ == "__main__":
    unittest.main()
