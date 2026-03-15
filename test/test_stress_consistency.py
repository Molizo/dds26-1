import random
import unittest
from concurrent.futures import ThreadPoolExecutor

import requests

import utils as tu
from common.constants import STATUS_ABORTED, STATUS_COMPLETED
from live_stack_utils import (
    GATEWAY_BASE_URL,
    LiveStackTestCase,
    get_active_tx_guards,
    list_order_transactions,
    run_compose,
    wait_gateway_ready,
    wait_until,
)


def _checkout_status(order_id: str) -> int:
    response = requests.post(
        f"{GATEWAY_BASE_URL}/orders/checkout/{order_id}",
        timeout=60,
    )
    return response.status_code


def _wait_public_routes_ready() -> None:
    def _ready():
        try:
            stock = requests.get(f"{GATEWAY_BASE_URL}/stock/find/does-not-exist", timeout=2)
            payment = requests.get(f"{GATEWAY_BASE_URL}/payment/find_user/does-not-exist", timeout=2)
            order = requests.get(f"{GATEWAY_BASE_URL}/orders/find/does-not-exist", timeout=2)
        except requests.RequestException:
            return False
        return all(400 <= response.status_code < 500 for response in (stock, payment, order))

    wait_until(
        _ready,
        timeout_seconds=20.0,
        message="Timed out waiting for stock, payment, and order routes to become reachable",
        interval_seconds=0.5,
    )


def _get_json_with_retry(path: str, timeout_seconds: float = 20.0) -> dict:
    def _fetch():
        try:
            response = requests.get(f"{GATEWAY_BASE_URL}{path}", timeout=5)
        except requests.RequestException:
            return None
        if response.status_code >= 500:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    return wait_until(
        _fetch,
        timeout_seconds=timeout_seconds,
        message=f"Timed out fetching JSON response for {path}",
        interval_seconds=0.5,
    )


def _add_item_to_order_with_retry(
    order_id: str,
    item_id: str,
    quantity: int,
    timeout_seconds: float = 30.0,
) -> None:
    def _try_add():
        try:
            response = requests.post(
                f"{GATEWAY_BASE_URL}/orders/addItem/{order_id}/{item_id}/{quantity}",
                timeout=5,
            )
        except requests.RequestException:
            return None
        if tu.status_code_is_success(response.status_code):
            return True
        return None

    wait_until(
        _try_add,
        timeout_seconds=timeout_seconds,
        message=f"Timed out adding {(item_id, quantity)} to order {order_id}",
        interval_seconds=0.5,
    )

class TestStressConsistency(LiveStackTestCase):
    def setUp(self):
        run_compose("start", "order-service", "stock-service", "payment-service")
        wait_gateway_ready()
        _wait_public_routes_ready()

    def test_concurrent_checkouts_preserve_resource_invariants(self):
        rng = random.Random(26)

        item_specs = [(5, 18), (7, 15), (11, 12)]
        item_prices: dict[str, int] = {}
        initial_stock: dict[str, int] = {}
        item_ids: list[str] = []
        for price, stock in item_specs:
            item_id = tu.create_item(price)["item_id"]
            self.assertTrue(tu.status_code_is_success(tu.add_stock(item_id, stock)))
            item_prices[item_id] = price
            initial_stock[item_id] = stock
            item_ids.append(item_id)

        user_ids: list[str] = []
        initial_credit: dict[str, int] = {}
        for _ in range(4):
            user_id = tu.create_user()["user_id"]
            self.assertTrue(tu.status_code_is_success(tu.add_credit_to_user(user_id, 80)))
            initial_credit[user_id] = 80
            user_ids.append(user_id)

        orders: list[dict] = []
        for _ in range(24):
            user_id = rng.choice(user_ids)
            order_id = tu.create_order(user_id)["order_id"]

            line_count = 1 if rng.random() < 0.5 else 2
            chosen_items = rng.sample(item_ids, k=line_count)
            line_items: list[tuple[str, int]] = []
            total_cost = 0

            for item_id in chosen_items:
                quantity = rng.randint(1, 3)
                _add_item_to_order_with_retry(order_id, item_id, quantity)
                line_items.append((item_id, quantity))
                total_cost += item_prices[item_id] * quantity

            orders.append(
                {
                    "order_id": order_id,
                    "user_id": user_id,
                    "line_items": line_items,
                    "total_cost": total_cost,
                }
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {
                executor.submit(_checkout_status, order["order_id"]): order
                for order in orders
            }
            for future, order in futures.items():
                order["status_code"] = future.result()

        order_ids = [order["order_id"] for order in orders]

        def _all_transactions_terminal():
            txs = list_order_transactions(order_ids)
            return (
                len(txs) == len(order_ids)
                and all(tx["status"] in {STATUS_COMPLETED, STATUS_ABORTED} for tx in txs)
            )

        wait_until(
            _all_transactions_terminal,
            timeout_seconds=20.0,
            message="Not all stress-test transactions converged to terminal states",
            interval_seconds=1.0,
        )

        final_orders: dict[str, dict] = {}
        for order in orders:
            final_orders[order["order_id"]] = _get_json_with_retry(
                f"/orders/find/{order['order_id']}"
            )

        expected_credit = dict(initial_credit)
        expected_stock = dict(initial_stock)
        paid_order_count = 0
        for order in orders:
            final_order = final_orders[order["order_id"]]
            if final_order["paid"]:
                paid_order_count += 1
                expected_credit[order["user_id"]] -= order["total_cost"]
                for item_id, quantity in order["line_items"]:
                    expected_stock[item_id] -= quantity
                self.assertEqual(
                    order["status_code"],
                    200,
                    f"Paid order {order['order_id']} must have a success checkout status",
                )
            else:
                self.assertTrue(
                    tu.status_code_is_failure(order["status_code"]),
                    f"Unpaid order {order['order_id']} must have a 4xx checkout status",
                )

        self.assertGreater(
            paid_order_count,
            0,
            "Stress harness should commit at least one order to exercise invariants",
        )

        for user_id, expected in expected_credit.items():
            actual = _get_json_with_retry(f"/payment/find_user/{user_id}")["credit"]
            self.assertGreaterEqual(actual, 0, f"User {user_id} credit became negative")
            self.assertEqual(
                actual,
                expected,
                f"User {user_id} credit drifted from committed-order totals",
            )

        for item_id, expected in expected_stock.items():
            actual = _get_json_with_retry(f"/stock/find/{item_id}")["stock"]
            self.assertGreaterEqual(actual, 0, f"Item {item_id} stock became negative")
            self.assertEqual(
                actual,
                expected,
                f"Item {item_id} stock drifted from committed-order totals",
            )

        txs = list_order_transactions(order_ids)
        self.assertEqual(len(txs), len(order_ids))
        self.assertTrue(
            all(tx["status"] in {STATUS_COMPLETED, STATUS_ABORTED} for tx in txs),
            f"Found non-terminal txs after stress run: {txs}",
        )

        active_guards = get_active_tx_guards(order_ids)
        self.assertTrue(
            all(value is None for value in active_guards.values()),
            f"Found leaked active guards after stress run: {active_guards}",
        )


if __name__ == "__main__":
    unittest.main()
