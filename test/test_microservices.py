import concurrent.futures
import threading
import time
import unittest
import uuid

import utils as tu


class TestCheckoutRegression(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        tu.wait_for_gateway()

    def test_checkout_happy_path_commits_once(self):
        user = tu.create_user_with_credit(100)
        user_id = user["user_id"]

        item_1 = tu.create_item_with_stock(price=4, stock=10)
        item_2 = tu.create_item_with_stock(price=6, stock=10)

        order = tu.create_order_with_lines(
            user_id,
            [
                (item_1["item_id"], 2),
                (item_2["item_id"], 1),
            ],
        )
        order_id = order["order_id"]

        response = tu.checkout(order_id)
        tu.assert_success(response)

        order_state = tu.get_order_state(order_id)
        self.assertTrue(order_state["paid"])
        self.assertEqual(order_state["total_cost"], 14)

        self.assertEqual(tu.find_item(item_1["item_id"])["stock"], 8)
        self.assertEqual(tu.find_item(item_2["item_id"])["stock"], 9)
        self.assertEqual(tu.find_user(user_id)["credit"], 86)

    def test_checkout_out_of_stock_rolls_back_all_items(self):
        user = tu.create_user_with_credit(100)
        user_id = user["user_id"]

        item_1 = tu.create_item_with_stock(price=5, stock=5)
        item_2 = tu.create_item_with_stock(price=3, stock=0)

        order = tu.create_order_with_lines(
            user_id,
            [
                (item_1["item_id"], 2),
                (item_2["item_id"], 1),
            ],
        )
        order_id = order["order_id"]

        initial_credit = tu.find_user(user_id)["credit"]
        initial_stock_1 = tu.find_item(item_1["item_id"])["stock"]
        initial_stock_2 = tu.find_item(item_2["item_id"])["stock"]

        response = tu.checkout(order_id)
        tu.assert_failure(response)

        self.assertEqual(tu.find_item(item_1["item_id"])["stock"], initial_stock_1)
        self.assertEqual(tu.find_item(item_2["item_id"])["stock"], initial_stock_2)
        self.assertEqual(tu.find_user(user_id)["credit"], initial_credit)
        self.assertFalse(tu.get_order_state(order_id)["paid"])

    def test_checkout_insufficient_credit_rolls_back_stock(self):
        user = tu.create_user_with_credit(4)
        user_id = user["user_id"]

        item_1 = tu.create_item_with_stock(price=3, stock=20)
        item_2 = tu.create_item_with_stock(price=2, stock=20)

        order = tu.create_order_with_lines(
            user_id,
            [
                (item_1["item_id"], 2),
                (item_2["item_id"], 2),
            ],
        )
        order_id = order["order_id"]

        initial_credit = tu.find_user(user_id)["credit"]
        initial_stock_1 = tu.find_item(item_1["item_id"])["stock"]
        initial_stock_2 = tu.find_item(item_2["item_id"])["stock"]

        response = tu.checkout(order_id)
        tu.assert_failure(response)

        self.assertEqual(tu.find_item(item_1["item_id"])["stock"], initial_stock_1)
        self.assertEqual(tu.find_item(item_2["item_id"])["stock"], initial_stock_2)
        self.assertEqual(tu.find_user(user_id)["credit"], initial_credit)
        self.assertFalse(tu.get_order_state(order_id)["paid"])

    def test_add_non_existing_item_fails_without_order_change(self):
        user = tu.create_user()
        order = tu.create_order(user["user_id"])

        order_id = order["order_id"]
        order_before = tu.get_order_state(order_id)
        missing_item_id = str(uuid.uuid4())

        response_code = tu.add_item_to_order(order_id, missing_item_id, 1)
        tu.assert_failure(response_code)

        order_after = tu.get_order_state(order_id)
        self.assertEqual(order_after["items"], order_before["items"])
        self.assertEqual(order_after["total_cost"], order_before["total_cost"])

    def test_add_item_negative_quantity_rejected_no_state_change(self):
        user = tu.create_user()
        order = tu.create_order(user["user_id"])
        order_id = order["order_id"]

        item = tu.create_item_with_stock(price=4, stock=10)
        item_id = item["item_id"]

        order_before = tu.get_order_state(order_id)

        response_code = tu.add_item_to_order(order_id, item_id, -1)
        tu.assert_failure(response_code)

        order_after = tu.get_order_state(order_id)
        self.assertEqual(order_after["items"], order_before["items"])
        self.assertEqual(order_after["total_cost"], order_before["total_cost"])

    def test_stock_add_negative_amount_rejected(self):
        item = tu.create_item_with_stock(price=5, stock=6)
        item_id = item["item_id"]

        stock_before = tu.find_item(item_id)["stock"]
        response_code = tu.add_stock(item_id, -2)
        tu.assert_failure(response_code)
        self.assertEqual(tu.find_item(item_id)["stock"], stock_before)

    def test_stock_subtract_negative_amount_rejected(self):
        item = tu.create_item_with_stock(price=5, stock=6)
        item_id = item["item_id"]

        stock_before = tu.find_item(item_id)["stock"]
        response_code = tu.subtract_stock(item_id, -2)
        tu.assert_failure(response_code)
        self.assertEqual(tu.find_item(item_id)["stock"], stock_before)

    def test_payment_add_funds_negative_amount_rejected(self):
        user = tu.create_user_with_credit(10)
        user_id = user["user_id"]

        credit_before = tu.find_user(user_id)["credit"]
        response_code = tu.add_credit_to_user(user_id, -3)
        tu.assert_failure(response_code)
        self.assertEqual(tu.find_user(user_id)["credit"], credit_before)

    def test_payment_pay_negative_amount_rejected_no_credit_increase(self):
        user = tu.create_user_with_credit(10)
        user_id = user["user_id"]

        credit_before = tu.find_user(user_id)["credit"]
        response_code = tu.payment_pay(user_id, -3)
        tu.assert_failure(response_code)
        self.assertEqual(tu.find_user(user_id)["credit"], credit_before)

    def test_create_order_unknown_user_rejected(self):
        unknown_user_id = str(uuid.uuid4())

        create_response = tu._request_with_retry(
            "POST",
            f"{tu.ORDER_URL}/orders/create/{unknown_user_id}",
        )
        tu.assert_failure(create_response)

    def test_checkout_invalid_order_id_has_no_side_effects(self):
        user = tu.create_user_with_credit(11)
        user_id = user["user_id"]

        item = tu.create_item_with_stock(price=5, stock=7)
        item_id = item["item_id"]

        initial_credit = tu.find_user(user_id)["credit"]
        initial_stock = tu.find_item(item_id)["stock"]

        response = tu.checkout(str(uuid.uuid4()))
        tu.assert_failure(response)

        self.assertEqual(tu.find_user(user_id)["credit"], initial_credit)
        self.assertEqual(tu.find_item(item_id)["stock"], initial_stock)

    def test_checkout_business_failure_must_be_4xx(self):
        user = tu.create_user_with_credit(1)
        user_id = user["user_id"]

        item = tu.create_item_with_stock(price=10, stock=1)
        item_id = item["item_id"]

        order = tu.create_order_with_lines(user_id, [(item_id, 1)])
        order_id = order["order_id"]

        response = tu.checkout(order_id)
        self.assertFalse(tu.status_code_is_success(response.status_code))
        tu.assert_failure(response)

    def test_duplicate_checkout_attempt_has_no_second_commit(self):
        user = tu.create_user_with_credit(20)
        user_id = user["user_id"]

        item = tu.create_item_with_stock(price=5, stock=4)
        item_id = item["item_id"]

        order = tu.create_order_with_lines(user_id, [(item_id, 2)])
        order_id = order["order_id"]

        first_response = tu.checkout(order_id)
        tu.assert_success(first_response)

        stock_before_second = tu.find_item(item_id)["stock"]
        credit_before_second = tu.find_user(user_id)["credit"]

        second_response = tu.checkout(order_id)
        tu.assert_success(second_response)
        self.assertEqual(tu.find_item(item_id)["stock"], stock_before_second)
        self.assertEqual(tu.find_user(user_id)["credit"], credit_before_second)

    def test_repeated_same_item_lines_are_aggregated(self):
        user = tu.create_user_with_credit(100)
        user_id = user["user_id"]

        item = tu.create_item_with_stock(price=4, stock=20)
        item_id = item["item_id"]

        order = tu.create_order_with_lines(
            user_id,
            [
                (item_id, 1),
                (item_id, 2),
                (item_id, 3),
            ],
        )
        order_id = order["order_id"]

        response = tu.checkout(order_id)
        tu.assert_success(response)

        order_state = tu.get_order_state(order_id)
        self.assertEqual(order_state["total_cost"], 24)
        self.assertEqual(tu.find_item(item_id)["stock"], 14)
        self.assertEqual(tu.find_user(user_id)["credit"], 76)

    def test_concurrent_inverse_item_order_contention_has_no_deadlock(self):
        user_1 = tu.create_user_with_credit(10)
        user_2 = tu.create_user_with_credit(10)

        item_a = tu.create_item_with_stock(price=2, stock=1)
        item_b = tu.create_item_with_stock(price=3, stock=1)

        order_1 = tu.create_order_with_lines(
            user_1["user_id"],
            [
                (item_a["item_id"], 1),
                (item_b["item_id"], 1),
            ],
        )
        order_2 = tu.create_order_with_lines(
            user_2["user_id"],
            [
                (item_b["item_id"], 1),
                (item_a["item_id"], 1),
            ],
        )

        barrier = threading.Barrier(2)

        def run_checkout(order_id: str) -> int:
            barrier.wait()
            return tu.checkout(order_id).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_1 = pool.submit(run_checkout, order_1["order_id"])
            future_2 = pool.submit(run_checkout, order_2["order_id"])
            status_1 = future_1.result(timeout=8)
            status_2 = future_2.result(timeout=8)

        self.assertTrue(tu.status_code_is_success(status_1) or tu.status_code_is_failure(status_1))
        self.assertTrue(tu.status_code_is_success(status_2) or tu.status_code_is_failure(status_2))

        success_count = sum(
            1 for status in [status_1, status_2] if tu.status_code_is_success(status)
        )
        self.assertLessEqual(success_count, 1)

        final_item_a_stock = tu.find_item(item_a["item_id"])["stock"]
        final_item_b_stock = tu.find_item(item_b["item_id"])["stock"]
        self.assertEqual(final_item_a_stock, 1 - success_count)
        self.assertEqual(final_item_b_stock, 1 - success_count)

        total_credit = tu.find_user(user_1["user_id"])["credit"] + tu.find_user(user_2["user_id"])["credit"]
        self.assertEqual(total_credit, 20 - (5 * success_count))

        paid_count = sum(
            [
                1 if tu.get_order_state(order_1["order_id"])["paid"] else 0,
                1 if tu.get_order_state(order_2["order_id"])["paid"] else 0,
            ]
        )
        self.assertEqual(paid_count, success_count)

    def test_concurrent_checkout_same_order_has_single_logical_commit(self):
        user = tu.create_user_with_credit(7)
        user_id = user["user_id"]

        item = tu.create_item_with_stock(price=7, stock=1)
        item_id = item["item_id"]

        order = tu.create_order_with_lines(user_id, [(item_id, 1)])
        order_id = order["order_id"]

        barrier = threading.Barrier(2)

        def run_checkout() -> int:
            barrier.wait()
            return tu.checkout(order_id).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_checkout) for _ in range(2)]
            statuses = [future.result() for future in futures]

        success_count = sum(1 for status in statuses if tu.status_code_is_success(status))
        failure_count = sum(1 for status in statuses if tu.status_code_is_failure(status))

        self.assertGreaterEqual(success_count, 1)
        self.assertLessEqual(success_count, 2)
        self.assertIn(failure_count, [0, 1])

        self.assertEqual(tu.find_user(user_id)["credit"], 0)
        self.assertEqual(tu.find_item(item_id)["stock"], 0)
        self.assertTrue(tu.get_order_state(order_id)["paid"])

    def test_parallel_non_conflicting_checkouts_complete_quickly_and_both_succeed(self):
        user_1 = tu.create_user_with_credit(20)
        user_2 = tu.create_user_with_credit(20)

        item_1 = tu.create_item_with_stock(price=4, stock=1)
        item_2 = tu.create_item_with_stock(price=5, stock=1)

        order_1 = tu.create_order_with_lines(user_1["user_id"], [(item_1["item_id"], 1)])
        order_2 = tu.create_order_with_lines(user_2["user_id"], [(item_2["item_id"], 1)])

        barrier = threading.Barrier(2)

        def run_checkout(order_id: str) -> int:
            barrier.wait()
            return tu.checkout(order_id).status_code

        started_at = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_1 = pool.submit(run_checkout, order_1["order_id"])
            future_2 = pool.submit(run_checkout, order_2["order_id"])
            status_1 = future_1.result(timeout=8)
            status_2 = future_2.result(timeout=8)
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 8)
        self.assertTrue(tu.status_code_is_success(status_1))
        self.assertTrue(tu.status_code_is_success(status_2))

        self.assertEqual(tu.find_item(item_1["item_id"])["stock"], 0)
        self.assertEqual(tu.find_item(item_2["item_id"])["stock"], 0)
        self.assertEqual(tu.find_user(user_1["user_id"])["credit"], 16)
        self.assertEqual(tu.find_user(user_2["user_id"])["credit"], 15)
        self.assertTrue(tu.get_order_state(order_1["order_id"])["paid"])
        self.assertTrue(tu.get_order_state(order_2["order_id"])["paid"])


if __name__ == "__main__":
    unittest.main()
