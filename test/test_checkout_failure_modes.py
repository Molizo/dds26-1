import base64
import os
import shutil
import subprocess
import threading
import time
import unittest

import requests

import utils as tu
from common.constants import CMD_HOLD, CMD_RELEASE, STOCK_COMMANDS_QUEUE
from common.models import decode_command


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOCKER_COMPOSE_CMD = ["docker", "compose"]
GATEWAY_BASE_URL = "http://127.0.0.1:8000"
RABBITMQ_API_BASE_URL = "http://127.0.0.1:15672/api"
RABBITMQ_AUTH = ("guest", "guest")
HOLD_TIMEOUT_SECONDS = 10.0
RELEASE_TIMEOUT_SECONDS = 10.0


def _run_compose(*args: str) -> None:
    result = subprocess.run(
        [*DOCKER_COMPOSE_CMD, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"docker compose {' '.join(args)} failed:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _wait_stock_route_ready(timeout_seconds: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"{GATEWAY_BASE_URL}/stock/find/does-not-exist"
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=2)
            # A 4xx from the stock route confirms gateway + stock are both serving.
            if 400 <= response.status_code < 500:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for stock service to become reachable via gateway")


def _rabbitmq_api(method: str, path: str, json_body: dict | None = None) -> requests.Response:
    response = requests.request(
        method=method,
        url=f"{RABBITMQ_API_BASE_URL}{path}",
        auth=RABBITMQ_AUTH,
        json=json_body,
        timeout=10,
    )
    response.raise_for_status()
    return response


def _purge_queue(queue_name: str) -> None:
    _rabbitmq_api("DELETE", f"/queues/%2F/{queue_name}/contents")


def _queue_consumers(queue_name: str) -> int:
    payload = _rabbitmq_api("GET", f"/queues/%2F/{queue_name}").json()
    return int(payload.get("consumers", 0))


def _wait_for_queue_consumers(queue_name: str, expected: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _queue_consumers(queue_name) == expected:
            return
        time.sleep(0.25)
    raise AssertionError(
        f"Timed out waiting for {queue_name} consumers == {expected} "
        f"(last={_queue_consumers(queue_name)})"
    )


def _wait_for_min_queue_consumers(queue_name: str, minimum: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _queue_consumers(queue_name) >= minimum:
            return
        time.sleep(0.25)
    raise AssertionError(
        f"Timed out waiting for {queue_name} consumers >= {minimum} "
        f"(last={_queue_consumers(queue_name)})"
    )


def _peek_queue(queue_name: str, count: int = 20) -> list[dict]:
    return _rabbitmq_api(
        "POST",
        f"/queues/%2F/{queue_name}/get",
        json_body={
            "count": count,
            "ackmode": "ack_requeue_true",
            "encoding": "base64",
            "truncate": 500000,
        },
    ).json()


def _publish_message_base64(queue_name: str, payload_base64: str) -> None:
    routed = _rabbitmq_api(
        "POST",
        "/exchanges/%2F/amq.default/publish",
        json_body={
            "properties": {},
            "routing_key": queue_name,
            "payload": payload_base64,
            "payload_encoding": "base64",
        },
    ).json().get("routed", False)
    if not routed:
        raise AssertionError(f"RabbitMQ refused to route message to queue {queue_name}")


def _decode_command_payload(message: dict) -> str:
    raw_payload = base64.b64decode(message["payload"])
    cmd = decode_command(raw_payload)
    return cmd.command


class TestCheckoutFailureModes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker CLI not found; these tests require Docker deployment")
        try:
            requests.get(f"{GATEWAY_BASE_URL}/stock/find/does-not-exist", timeout=3)
            requests.get(f"{RABBITMQ_API_BASE_URL}/overview", auth=RABBITMQ_AUTH, timeout=3)
        except requests.RequestException as exc:
            raise unittest.SkipTest(
                "Gateway/RabbitMQ not reachable. Start the stack with `docker compose up --build` first."
            ) from exc

    def setUp(self):
        _run_compose("start", "order-service", "orchestrator-service", "stock-service", "payment-service")
        _wait_stock_route_ready()
        _purge_queue(STOCK_COMMANDS_QUEUE)

    def tearDown(self):
        _run_compose("start", "order-service", "orchestrator-service", "stock-service", "payment-service")
        _wait_stock_route_ready()
        _purge_queue(STOCK_COMMANDS_QUEUE)

    def test_active_guard_stays_locked_when_checkout_needs_recovery(self):
        # Rationale: the route must keep order_active_tx set when the first checkout
        # ended in FAILED_NEEDS_RECOVERY, otherwise a normal user retry can start a
        # second transaction on the same order before recovery finishes.
        user_id = tu.create_user()["user_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_credit_to_user(user_id, 100)))

        item_id = tu.create_item(7)["item_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_stock(item_id, 10)))

        order_id = tu.create_order(user_id)["order_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_item_to_order(order_id, item_id, 1)))

        _run_compose("stop", "stock-service")
        _wait_for_queue_consumers(STOCK_COMMANDS_QUEUE, expected=0, timeout_seconds=30.0)
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
            _run_compose("start", "stock-service")
            _wait_stock_route_ready()

        self.assertEqual(
            second_response.status_code,
            409,
            "Retry checkout must be rejected with 409 while previous tx still needs recovery.",
        )
        self.assertLess(
            retry_elapsed,
            5.0,
            f"Guard rejection should be immediate; retry took {retry_elapsed:.2f}s",
        )

    def test_stale_hold_reply_must_not_complete_release_phase(self):
        # Rationale: hold/commit/release currently reuse the same tx_id. A delayed hold
        # reply must not be accepted during release waiting, otherwise release can appear
        # complete from mixed-phase replies.
        user_id = tu.create_user()["user_id"]

        item_id = tu.create_item(5)["item_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_stock(item_id, 5)))

        order_id = tu.create_order(user_id)["order_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_item_to_order(order_id, item_id, 1)))

        _run_compose("stop", "stock-service")
        _wait_for_queue_consumers(STOCK_COMMANDS_QUEUE, expected=0, timeout_seconds=30.0)
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
            # Wait until both stock messages (hold + release) are queued while the stock
            # service is stopped. Then remove release and re-inject only hold, so the
            # next stock reply is guaranteed to be stale relative to release phase.
            deadline = time.monotonic() + HOLD_TIMEOUT_SECONDS + 15.0
            queued_messages: list[dict] = []
            while time.monotonic() < deadline:
                queued_messages = _peek_queue(STOCK_COMMANDS_QUEUE, count=20)
                if len(queued_messages) >= 2:
                    break
                time.sleep(0.25)
            self.assertGreaterEqual(
                len(queued_messages),
                2,
                f"Expected at least hold+release commands in queue, got {len(queued_messages)}",
            )

            hold_payload: str | None = None
            seen_commands: list[str] = []
            for message in queued_messages:
                command = _decode_command_payload(message)
                seen_commands.append(command)
                if command == CMD_HOLD and hold_payload is None:
                    hold_payload = message["payload"]

            self.assertIn(CMD_HOLD, seen_commands, f"Queue commands missing hold: {seen_commands}")
            self.assertIn(
                CMD_RELEASE,
                seen_commands,
                f"Queue commands missing release: {seen_commands}",
            )
            if hold_payload is None:
                raise AssertionError("Could not capture hold payload for stale-reply replay")

            _purge_queue(STOCK_COMMANDS_QUEUE)
            _publish_message_base64(STOCK_COMMANDS_QUEUE, hold_payload)

            _run_compose("start", "stock-service")
            _wait_for_min_queue_consumers(STOCK_COMMANDS_QUEUE, minimum=1, timeout_seconds=30.0)
            _wait_stock_route_ready()

            thread.join(timeout=45)
            self.assertFalse(thread.is_alive(), "Checkout thread did not finish in time")
        finally:
            _run_compose("start", "stock-service")
            _wait_stock_route_ready()

        self.assertEqual(
            checkout_result["status_code"],
            400,
            f"Expected checkout failure path, got {checkout_result['status_code']}",
        )

        elapsed_seconds = float(checkout_result["elapsed_seconds"] or 0.0)
        expected_min = HOLD_TIMEOUT_SECONDS + RELEASE_TIMEOUT_SECONDS - 2.0
        self.assertGreaterEqual(
            elapsed_seconds,
            expected_min,
            (
                "Coordinator returned too early after injecting only a stale hold reply. "
                "Release phase should continue waiting for a release reply or timeout."
            ),
        )


if __name__ == "__main__":
    unittest.main()
