import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import redis

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from shared_messaging.redis_atomic import (
    ATOMIC_APPLIED,
    ATOMIC_BUSINESS_REJECT,
    ATOMIC_DUPLICATE,
    ATOMIC_MISSING_ENTITY,
    _eval_script,
    _map_result,
    charge_payment_atomic,
    refund_payment_atomic,
    release_stock_atomic,
    reserve_stock_atomic,
)


class FakeRedisNoScriptThenSuccess:
    def __init__(self):
        self.connection_pool = object()
        self.load_calls = 0
        self.eval_calls = 0

    def script_load(self, _body):
        self.load_calls += 1
        return "sha1"

    def evalsha(self, _sha, _numkeys, *_values):
        self.eval_calls += 1
        if self.eval_calls == 1:
            raise redis.exceptions.NoScriptError("NOSCRIPT")
        return 1


class TestPhase3RedisAtomicLayer(unittest.TestCase):
    def test_map_result_codes(self):
        self.assertEqual(_map_result(1).status, ATOMIC_APPLIED)
        self.assertEqual(_map_result(0).status, ATOMIC_DUPLICATE)
        self.assertEqual(_map_result(-1).status, ATOMIC_BUSINESS_REJECT)
        self.assertEqual(_map_result(-2).status, ATOMIC_MISSING_ENTITY)
        with self.assertRaises(ValueError):
            _map_result(999)

    def test_eval_script_recovers_from_noscript(self):
        fake = FakeRedisNoScriptThenSuccess()
        result = _eval_script(fake, "stock_reserve", keys=["k1", "k2", "k3"], args=[1, 2, 3])
        self.assertEqual(result, 1)
        self.assertEqual(fake.load_calls, 2)
        self.assertEqual(fake.eval_calls, 2)

    @patch("shared_messaging.redis_atomic._eval_script", return_value=1)
    def test_reserve_stock_atomic_uses_expected_keys(self, mocked_eval):
        reserve_stock_atomic(
            MagicMock(),
            service="stock-worker",
            message_id="m1",
            item_id="item-1",
            step="reserve",
            quantity=2,
        )
        self.assertEqual(mocked_eval.call_args.kwargs["keys"][0], "stock:item-1")
        self.assertEqual(mocked_eval.call_args.kwargs["keys"][1], "inbox:stock-worker:m1")
        self.assertEqual(mocked_eval.call_args.kwargs["keys"][2], "effects:stock-worker:item-1:reserve")

    @patch("shared_messaging.redis_atomic._eval_script", return_value=0)
    def test_release_stock_atomic_maps_duplicate(self, mocked_eval):
        result = release_stock_atomic(
            MagicMock(),
            service="stock-worker",
            message_id="m2",
            item_id="item-9",
            step="release",
            quantity=1,
        )
        self.assertEqual(result.status, ATOMIC_DUPLICATE)
        self.assertEqual(mocked_eval.call_args.args[1], "stock_release")

    @patch("shared_messaging.redis_atomic._eval_script", return_value=-1)
    def test_charge_payment_atomic_maps_business_reject(self, mocked_eval):
        result = charge_payment_atomic(
            MagicMock(),
            service="payment-worker",
            message_id="m3",
            user_id="u1",
            step="charge",
            amount=99,
        )
        self.assertEqual(result.status, ATOMIC_BUSINESS_REJECT)
        self.assertEqual(mocked_eval.call_args.args[1], "payment_charge")

    @patch("shared_messaging.redis_atomic._eval_script", return_value=-2)
    def test_refund_payment_atomic_maps_missing_entity(self, mocked_eval):
        result = refund_payment_atomic(
            MagicMock(),
            service="payment-worker",
            message_id="m4",
            user_id="u9",
            step="refund",
            amount=5,
        )
        self.assertEqual(result.status, ATOMIC_MISSING_ENTITY)
        self.assertEqual(mocked_eval.call_args.args[1], "payment_refund")


if __name__ == "__main__":
    unittest.main()
