import time
import unittest

import requests

import utils as tu
from common.constants import (
    PAYMENT_COMMANDS_DLQ,
    PAYMENT_COMMANDS_QUEUE,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED_NEEDS_RECOVERY,
    STOCK_COMMANDS_DLQ,
    STOCK_COMMANDS_QUEUE,
)
from live_stack_utils import (
    GATEWAY_BASE_URL,
    LiveStackTestCase,
    get_active_tx_guards,
    inspect_order_runtime,
    list_order_transactions,
    purge_queue,
    run_compose,
    wait_for_queue_consumers,
    wait_gateway_ready,
    wait_until,
)


class TestRecoveryConvergence(LiveStackTestCase):
    def setUp(self):
        run_compose("start", "order-service", "stock-service", "payment-service")
        wait_gateway_ready()

        for queue_name in (
            STOCK_COMMANDS_QUEUE,
            PAYMENT_COMMANDS_QUEUE,
            STOCK_COMMANDS_DLQ,
            PAYMENT_COMMANDS_DLQ,
        ):
            purge_queue(queue_name)

        wait_for_queue_consumers(STOCK_COMMANDS_QUEUE, minimum=1, timeout_seconds=30.0)
        wait_for_queue_consumers(PAYMENT_COMMANDS_QUEUE, minimum=1, timeout_seconds=30.0)

    def tearDown(self):
        run_compose("start", "order-service", "stock-service", "payment-service")
        wait_gateway_ready()

    def test_failed_checkout_recovers_to_aborted_then_allows_clean_retry(self):
        user_id = tu.create_user()["user_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_credit_to_user(user_id, 100)))

        item_id = tu.create_item(5)["item_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_stock(item_id, 6)))

        order_id = tu.create_order(user_id)["order_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_item_to_order(order_id, item_id, 1)))

        run_compose("stop", "stock-service")
        wait_for_queue_consumers(STOCK_COMMANDS_QUEUE, expected=0, timeout_seconds=30.0)

        try:
            first_response = requests.post(
                f"{GATEWAY_BASE_URL}/orders/checkout/{order_id}",
                timeout=45,
            )
        finally:
            run_compose("start", "stock-service")
            wait_for_queue_consumers(STOCK_COMMANDS_QUEUE, minimum=1, timeout_seconds=30.0)
            wait_gateway_ready()

        self.assertTrue(
            tu.status_code_is_failure(first_response.status_code),
            f"Expected checkout failure during induced participant outage, got {first_response.status_code}",
        )

        wait_until(
            lambda: len(list_order_transactions([order_id])) == 1,
            timeout_seconds=10.0,
            message="Checkout tx record was not persisted for the failed order",
        )
        initial_txs = list_order_transactions([order_id])
        self.assertEqual(len(initial_txs), 1)

        failed_tx = initial_txs[0]
        self.assertEqual(failed_tx["status"], STATUS_FAILED_NEEDS_RECOVERY)

        failed_runtime = inspect_order_runtime(order_id, failed_tx["tx_id"])
        self.assertEqual(failed_runtime["tx_status"], STATUS_FAILED_NEEDS_RECOVERY)
        self.assertEqual(
            failed_runtime["active_guard"],
            failed_tx["tx_id"],
            "The active guard must stay attached to the non-terminal tx until recovery finishes",
        )
        self.assertFalse(bool(failed_runtime["order_paid"]))
        self.assertEqual(tu.find_item(item_id)["stock"], 6)
        self.assertEqual(tu.find_user(user_id)["credit"], 100)

        # Recovery scans only stale txs. Sleeping past the stale-age threshold and
        # then restarting order-service triggers an immediate startup scan.
        time.sleep(16.0)
        run_compose("restart", "order-service")
        wait_gateway_ready(timeout_seconds=60.0)

        def _aborted_and_clean():
            txs = list_order_transactions([order_id])
            if len(txs) != 1:
                return False
            guards = get_active_tx_guards([order_id])
            return txs[0]["status"] == STATUS_ABORTED and guards.get(order_id) is None

        wait_until(
            _aborted_and_clean,
            timeout_seconds=45.0,
            message="Recovery did not converge the failed tx to ABORTED with guard cleanup",
            interval_seconds=1.0,
        )

        recovered_runtime = inspect_order_runtime(order_id, failed_tx["tx_id"])
        self.assertEqual(recovered_runtime["tx_status"], STATUS_ABORTED)
        self.assertIsNone(recovered_runtime["active_guard"])
        self.assertFalse(bool(recovered_runtime["order_paid"]))
        self.assertEqual(tu.find_item(item_id)["stock"], 6)
        self.assertEqual(tu.find_user(user_id)["credit"], 100)

        retry_response = requests.post(
            f"{GATEWAY_BASE_URL}/orders/checkout/{order_id}",
            timeout=45,
        )
        self.assertEqual(retry_response.status_code, 200, retry_response.text)

        wait_until(
            lambda: any(
                tx["status"] == STATUS_COMPLETED
                for tx in list_order_transactions([order_id])
            ),
            timeout_seconds=10.0,
            message="Successful retry did not produce a terminal completed tx",
        )

        final_txs = list_order_transactions([order_id])
        self.assertEqual(
            sorted(tx["status"] for tx in final_txs),
            [STATUS_ABORTED, STATUS_COMPLETED],
        )
        self.assertEqual(get_active_tx_guards([order_id])[order_id], None)

        final_order = tu.find_order(order_id)
        self.assertTrue(final_order["paid"])
        self.assertEqual(tu.find_item(item_id)["stock"], 5)
        self.assertEqual(tu.find_user(user_id)["credit"], 95)


if __name__ == "__main__":
    unittest.main()
