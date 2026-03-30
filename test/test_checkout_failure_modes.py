import base64
import threading
import time
import unittest

import requests

import utils as tu
from common.constants import CMD_HOLD, CMD_RELEASE, STOCK_COMMANDS_QUEUE
from common.models import decode_command
from live_stack_utils import (
    GATEWAY_BASE_URL,
    LiveStackTestCase,
    get_queue_messages,
    publish_base64,
    purge_queue,
    run_compose,
    wait_for_queue_consumers,
    wait_gateway_ready,
)

HOLD_TIMEOUT_SECONDS = 10.0
RELEASE_TIMEOUT_SECONDS = 10.0


def _decode_command_payload(message: dict) -> str:
    raw_payload = base64.b64decode(message["payload"])
    cmd = decode_command(raw_payload)
    return cmd.command


class TestCheckoutFailureModes(LiveStackTestCase):
    def setUp(self):
        run_compose("start", "order-service", "orchestrator-service", "stock-service", "payment-service")
        wait_gateway_ready()
        purge_queue(STOCK_COMMANDS_QUEUE)

    def tearDown(self):
        run_compose("start", "order-service", "orchestrator-service", "stock-service", "payment-service")
        wait_gateway_ready()
        purge_queue(STOCK_COMMANDS_QUEUE)

    def test_active_guard_stays_locked_when_checkout_needs_recovery(self):
        user_id = tu.create_user()["user_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_credit_to_user(user_id, 100)))

        item_id = tu.create_item(7)["item_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_stock(item_id, 10)))

        order_id = tu.create_order(user_id)["order_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_item_to_order(order_id, item_id, 1)))

        run_compose("stop", "stock-service")
        wait_for_queue_consumers(
            STOCK_COMMANDS_QUEUE,
            expected=0,
            timeout_seconds=30.0,
        )
        try:
            first_response = requests.post(
                f"{GATEWAY_BASE_URL}/orders/checkout/{order_id}",
                timeout=45,
            )
            self.assertTrue(
                tu.status_code_is_failure(first_response.status_code),
                f"Expected first checkout failure while stock-service is stopped, got {first_response.status_code}",
            )

            retry_start = time.monotonic()
            second_response = requests.post(
                f"{GATEWAY_BASE_URL}/orders/checkout/{order_id}",
                timeout=45,
            )
            retry_elapsed = time.monotonic() - retry_start
        finally:
            run_compose("start", "stock-service")
            wait_gateway_ready()

        self.assertEqual(second_response.status_code, 409)
        self.assertLess(retry_elapsed, 5.0)

    def test_stale_hold_reply_must_not_complete_release_phase(self):
        user_id = tu.create_user()["user_id"]

        item_id = tu.create_item(5)["item_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_stock(item_id, 5)))

        order_id = tu.create_order(user_id)["order_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_item_to_order(order_id, item_id, 1)))

        run_compose("stop", "stock-service")
        wait_for_queue_consumers(
            STOCK_COMMANDS_QUEUE,
            expected=0,
            timeout_seconds=30.0,
        )
        checkout_result: dict[str, float | int | None] = {
            "status_code": None,
            "elapsed_seconds": None,
        }

        def _checkout_call() -> None:
            start = time.monotonic()
            response = requests.post(
                f"{GATEWAY_BASE_URL}/orders/checkout/{order_id}",
                timeout=60,
            )
            checkout_result["status_code"] = response.status_code
            checkout_result["elapsed_seconds"] = time.monotonic() - start

        thread = threading.Thread(target=_checkout_call, daemon=True)
        thread.start()

        try:
            deadline = time.monotonic() + HOLD_TIMEOUT_SECONDS + 15.0
            queued_messages: list[dict] = []
            while time.monotonic() < deadline:
                queued_messages = get_queue_messages(STOCK_COMMANDS_QUEUE, count=20, requeue=True)
                if len(queued_messages) >= 2:
                    break
                time.sleep(0.25)
            self.assertGreaterEqual(len(queued_messages), 2)

            hold_payload: str | None = None
            seen_commands: list[str] = []
            for message in queued_messages:
                command = _decode_command_payload(message)
                seen_commands.append(command)
                if command == CMD_HOLD and hold_payload is None:
                    hold_payload = message["payload"]

            self.assertIn(CMD_HOLD, seen_commands)
            self.assertIn(CMD_RELEASE, seen_commands)
            if hold_payload is None:
                raise AssertionError("Could not capture hold payload for stale-reply replay")

            purge_queue(STOCK_COMMANDS_QUEUE)
            publish_base64(STOCK_COMMANDS_QUEUE, hold_payload)

            run_compose("start", "stock-service")
            wait_for_queue_consumers(
                STOCK_COMMANDS_QUEUE,
                minimum=1,
                timeout_seconds=30.0,
            )
            wait_gateway_ready()

            thread.join(timeout=45)
            self.assertFalse(thread.is_alive(), "Checkout thread did not finish in time")
        finally:
            run_compose("start", "stock-service")
            wait_gateway_ready()

        self.assertEqual(checkout_result["status_code"], 400)

        elapsed_seconds = float(checkout_result["elapsed_seconds"] or 0.0)
        expected_min = HOLD_TIMEOUT_SECONDS + RELEASE_TIMEOUT_SECONDS - 2.0
        self.assertGreaterEqual(elapsed_seconds, expected_min)


if __name__ == "__main__":
    unittest.main()
