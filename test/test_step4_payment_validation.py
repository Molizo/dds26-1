import unittest
from unittest.mock import patch

from local_app_loader import load_payment_app


class TestPaymentValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payment_app, cls.payment_service = load_payment_app()

    def test_add_funds_rejects_non_positive_amount(self):
        with patch.object(self.payment_service, "add_credit", side_effect=AssertionError("should not call add_credit")):
            with self.payment_app.app.test_client() as client:
                zero_response = client.post("/add_funds/user-1/0")
                negative_response = client.post("/add_funds/user-1/-5")

        self.assertEqual(zero_response.status_code, 400)
        self.assertEqual(negative_response.status_code, 400)

    def test_pay_rejects_non_positive_amount(self):
        with patch.object(self.payment_service, "pay_credit", side_effect=AssertionError("should not call pay_credit")):
            with self.payment_app.app.test_client() as client:
                zero_response = client.post("/pay/user-1/0")
                negative_response = client.post("/pay/user-1/-5")

        self.assertEqual(zero_response.status_code, 400)
        self.assertEqual(negative_response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
