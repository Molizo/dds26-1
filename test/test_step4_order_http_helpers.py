import unittest
from unittest.mock import patch

import requests
from werkzeug.exceptions import HTTPException

from common.models import InternalReply
from local_app_loader import load_order_app


class TestOrderHttpHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.order_app, _ = load_order_app()

    def test_send_get_retries_then_aborts(self):
        with self.order_app.app.test_request_context():
            with patch.object(
                self.order_app._session,
                "get",
                side_effect=requests.exceptions.RequestException("boom"),
            ) as mock_get:
                with self.assertRaises(HTTPException) as exc:
                    self.order_app._send_get("http://example.test/find")

        self.assertEqual(exc.exception.code, 400)
        self.assertEqual(mock_get.call_count, self.order_app.REQUEST_RETRY_COUNT)

    def test_checkout_aborts_when_orchestrator_reply_times_out(self):
        with patch.object(
            self.order_app,
            "_lookup_order",
            return_value=self.order_app.OrderLookupResult(
                status="ok",
                order=self.order_app.OrderValue(
                    paid=False,
                    items=[],
                    user_id="user-1",
                    total_cost=0,
                ),
            ),
        ), patch.object(self.order_app, "db") as mock_db:
            mock_db.exists.return_value = 0
            with self.order_app.app.test_client() as client:
                with patch.object(self.order_app, "_call_orchestrator", return_value=None):
                    response = client.post("/checkout/order-1")

        self.assertEqual(response.status_code, 400)

    def test_checkout_returns_orchestrator_failure_status(self):
        with patch.object(
            self.order_app,
            "_lookup_order",
            return_value=self.order_app.OrderLookupResult(
                status="ok",
                order=self.order_app.OrderValue(
                    paid=False,
                    items=[],
                    user_id="user-1",
                    total_cost=0,
                ),
            ),
        ), patch.object(self.order_app, "db") as mock_db:
            mock_db.exists.return_value = 0
            with self.order_app.app.test_client() as client:
                with patch.object(
                    self.order_app,
                    "_call_orchestrator",
                    return_value=InternalReply(
                        request_id="req-1",
                        command="checkout",
                        ok=False,
                        status_code=409,
                        error="Checkout already in progress",
                    ),
                ):
                    response = client.post("/checkout/order-1")

        self.assertEqual(response.status_code, 409)

    def test_checkout_defaults_missing_orchestrator_status_code_to_400(self):
        with patch.object(
            self.order_app,
            "_lookup_order",
            return_value=self.order_app.OrderLookupResult(
                status="ok",
                order=self.order_app.OrderValue(
                    paid=False,
                    items=[],
                    user_id="user-1",
                    total_cost=0,
                ),
            ),
        ), patch.object(self.order_app, "db") as mock_db:
            mock_db.exists.return_value = 0
            with self.order_app.app.test_client() as client:
                with patch.object(
                    self.order_app,
                    "_call_orchestrator",
                    return_value=InternalReply(
                        request_id="req-1",
                        command="checkout",
                        ok=False,
                        status_code=None,
                        error="Checkout failed",
                    ),
                ):
                    response = client.post("/checkout/order-1")

        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
