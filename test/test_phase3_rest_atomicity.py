import os
import sys
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from shared_messaging.idempotency import (
    PROCESS_ACTION_ACK,
    PROCESS_ACTION_RETRY,
    PROCESS_STATUS_APPLIED,
    PROCESS_STATUS_BUSINESS_REJECT,
    PROCESS_STATUS_DUPLICATE,
    PROCESS_STATUS_INFRA_ERROR,
    process_idempotent_step,
)
from shared_messaging.redis_atomic import (
    ATOMIC_APPLIED,
    ATOMIC_BUSINESS_REJECT,
    ATOMIC_DUPLICATE,
    AtomicResult,
)


class TestPhase3RestAtomicity(unittest.TestCase):
    def test_process_idempotent_step_applied_ack(self):
        outcome = process_idempotent_step(
            apply_effect=lambda: AtomicResult(status=ATOMIC_APPLIED, raw_code=1)
        )
        self.assertEqual(outcome.action, PROCESS_ACTION_ACK)
        self.assertEqual(outcome.status, PROCESS_STATUS_APPLIED)

    def test_process_idempotent_step_duplicate_ack(self):
        outcome = process_idempotent_step(
            apply_effect=lambda: AtomicResult(status=ATOMIC_DUPLICATE, raw_code=0)
        )
        self.assertEqual(outcome.action, PROCESS_ACTION_ACK)
        self.assertEqual(outcome.status, PROCESS_STATUS_DUPLICATE)

    def test_process_idempotent_step_business_reject_ack(self):
        outcome = process_idempotent_step(
            apply_effect=lambda: AtomicResult(status=ATOMIC_BUSINESS_REJECT, raw_code=-1)
        )
        self.assertEqual(outcome.action, PROCESS_ACTION_ACK)
        self.assertEqual(outcome.status, PROCESS_STATUS_BUSINESS_REJECT)

    def test_process_idempotent_step_retries_on_apply_exception(self):
        outcome = process_idempotent_step(
            apply_effect=lambda: (_ for _ in ()).throw(RuntimeError("redis down"))
        )
        self.assertEqual(outcome.action, PROCESS_ACTION_RETRY)
        self.assertEqual(outcome.status, PROCESS_STATUS_INFRA_ERROR)
        self.assertIn("redis down", outcome.reason or "")

    def test_process_idempotent_step_retries_on_callback_exception(self):
        outcome = process_idempotent_step(
            apply_effect=lambda: AtomicResult(status=ATOMIC_APPLIED, raw_code=1),
            on_applied=lambda: (_ for _ in ()).throw(RuntimeError("publish failed")),
        )
        self.assertEqual(outcome.action, PROCESS_ACTION_RETRY)
        self.assertEqual(outcome.status, PROCESS_STATUS_INFRA_ERROR)
        self.assertIn("publish failed", outcome.reason or "")


if __name__ == "__main__":
    unittest.main()
