import unittest
from unittest.mock import patch

from local_app_loader import load_stock_app


class TestStockValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stock_app, cls.stock_service = load_stock_app()

    def test_create_item_rejects_non_positive_price(self):
        with self.stock_app.app.test_client() as client:
            zero_response = client.post("/item/create/0")
            negative_response = client.post("/item/create/-3")

        self.assertEqual(zero_response.status_code, 400)
        self.assertEqual(negative_response.status_code, 400)

    def test_add_stock_rejects_non_positive_amount(self):
        with patch.object(self.stock_service, "add_stock", side_effect=AssertionError("should not call add_stock")):
            with self.stock_app.app.test_client() as client:
                zero_response = client.post("/add/item-1/0")
                negative_response = client.post("/add/item-1/-5")

        self.assertEqual(zero_response.status_code, 400)
        self.assertEqual(negative_response.status_code, 400)

    def test_subtract_stock_rejects_non_positive_amount(self):
        with patch.object(self.stock_service, "subtract_stock", side_effect=AssertionError("should not call subtract_stock")):
            with self.stock_app.app.test_client() as client:
                zero_response = client.post("/subtract/item-1/0")
                negative_response = client.post("/subtract/item-1/-5")

        self.assertEqual(zero_response.status_code, 400)
        self.assertEqual(negative_response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
