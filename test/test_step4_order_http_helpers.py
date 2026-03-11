import unittest
from unittest.mock import patch

import requests
from werkzeug.exceptions import HTTPException

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

    def test_send_post_retries_then_aborts(self):
        with self.order_app.app.test_request_context():
            with patch.object(
                self.order_app._session,
                "post",
                side_effect=requests.exceptions.RequestException("boom"),
            ) as mock_post:
                with self.assertRaises(HTTPException) as exc:
                    self.order_app._send_post("http://example.test/pay")

        self.assertEqual(exc.exception.code, 400)
        self.assertEqual(mock_post.call_count, self.order_app.REQUEST_RETRY_COUNT)


if __name__ == '__main__':
    unittest.main()
