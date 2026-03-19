import os
import sys
import threading
import unittest
from unittest import mock

import redis
from msgspec import msgpack

from local_app_loader import load_order_app


_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content or b""
        self.headers = headers or {"Content-Type": "text/plain; charset=utf-8"}

    def json(self) -> dict:
        return dict(self._payload)


class _FakeWatchPipeline:
    def __init__(self, db):
        self._db = db
        self._watched_revisions = {}
        self._commands = []
        self._barrier_waited = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.reset()
        return False

    def watch(self, *keys):
        with self._db._lock:
            self._watched_revisions = {
                key: self._db._revisions.get(key, 0)
                for key in keys
            }

    def get(self, key):
        self._maybe_wait_on_barrier(key)
        return self._db.get(key)

    def exists(self, key):
        return self._db.exists(key)

    def multi(self):
        self._commands = []

    def set(self, key, value):
        self._commands.append(("set", key, value))

    def execute(self):
        with self._db._lock:
            for key, watched_revision in self._watched_revisions.items():
                if self._db._revisions.get(key, 0) != watched_revision:
                    raise redis.WatchError()

            for command, key, value in self._commands:
                if command == "set":
                    self._db._set_locked(key, value)
            self.reset()

    def unwatch(self):
        self.reset()

    def reset(self):
        self._watched_revisions = {}
        self._commands = []

    def _maybe_wait_on_barrier(self, key):
        barrier = self._db._claim_barrier(key)
        if barrier is None or self._barrier_waited:
            return
        self._barrier_waited = True
        barrier.wait(timeout=2)


class _FakeWatchRedis:
    def __init__(self):
        self._data = {}
        self._revisions = {}
        self._lock = threading.Lock()
        self._barrier_key = None
        self._barrier = None
        self._barrier_passes_remaining = 0

    def set(self, key, value, **kwargs):
        with self._lock:
            self._set_locked(key, value)
        return True

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def exists(self, key):
        with self._lock:
            return 1 if key in self._data else 0

    def pipeline(self, transaction=True):
        return _FakeWatchPipeline(self)

    def arm_barrier(self, key: str, parties: int):
        self._barrier_key = key
        self._barrier = threading.Barrier(parties)
        self._barrier_passes_remaining = parties

    def _claim_barrier(self, key):
        with self._lock:
            if key != self._barrier_key or self._barrier is None:
                return None
            if self._barrier_passes_remaining <= 0:
                return None
            self._barrier_passes_remaining -= 1
            return self._barrier

    def _set_locked(self, key, value):
        self._data[key] = value
        self._revisions[key] = self._revisions.get(key, 0) + 1


class TestOrderApiGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.order_app, cls.store_module = load_order_app()

    def _make_order_bytes(self, *, paid=False, items=None, total_cost=0):
        if items is None:
            items = []
        return msgpack.encode(
            self.store_module.OrderValue(
                paid=paid,
                items=items,
                user_id="user-1",
                total_cost=total_cost,
            )
        )

    def test_add_item_retries_after_watch_conflict(self):
        fake_db = _FakeWatchRedis()
        order_id = "order-1"
        item_id = "item-1"
        fake_db.set(order_id, self._make_order_bytes())
        fake_db.arm_barrier(order_id, parties=2)

        results = []

        def _request_add_item():
            with self.order_app.app.test_client() as client:
                response = client.post(f"/addItem/{order_id}/{item_id}/1")
                results.append(response.status_code)

        with mock.patch.object(self.order_app, "db", fake_db), \
             mock.patch.object(
                 self.order_app,
                 "_send_get",
                 return_value=_FakeResponse(200, {"price": 5}),
             ), \
             mock.patch.object(self.order_app, "_acquire_mutation_guard", return_value="lease"), \
             mock.patch.object(self.order_app, "_release_mutation_guard"):
            threads = [threading.Thread(target=_request_add_item) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(sorted(results), [200, 200])
        saved = msgpack.decode(fake_db.get(order_id), type=self.store_module.OrderValue)
        self.assertEqual(saved.items, [(item_id, 1), (item_id, 1)])
        self.assertEqual(saved.total_cost, 10)

    def test_add_item_rejects_non_positive_quantity(self):
        with mock.patch.object(
            self.order_app,
            "_send_get",
            side_effect=AssertionError("should not call stock"),
        ):
            with self.order_app.app.test_client() as client:
                zero_response = client.post("/addItem/order-1/item-1/0")
                negative_response = client.post("/addItem/order-1/item-1/-2")

        self.assertEqual(zero_response.status_code, 400)
        self.assertEqual(negative_response.status_code, 400)

    def test_add_item_rejects_paid_order(self):
        fake_db = _FakeWatchRedis()
        order_id = "order-paid"
        fake_db.set(order_id, self._make_order_bytes(paid=True, items=[("item-1", 1)], total_cost=5))

        with mock.patch.object(self.order_app, "db", fake_db), \
             mock.patch.object(
                 self.order_app,
                 "_send_get",
                 return_value=_FakeResponse(200, {"price": 5}),
             ), \
             mock.patch.object(self.order_app, "_acquire_mutation_guard", return_value="lease"), \
             mock.patch.object(self.order_app, "_release_mutation_guard"):
            with self.order_app.app.test_client() as client:
                response = client.post(f"/addItem/{order_id}/item-1/1")

        self.assertEqual(response.status_code, 409)
        saved = msgpack.decode(fake_db.get(order_id), type=self.store_module.OrderValue)
        self.assertEqual(saved.items, [("item-1", 1)])
        self.assertEqual(saved.total_cost, 5)

    def test_add_item_rejects_order_with_active_checkout_guard(self):
        fake_db = _FakeWatchRedis()
        order_id = "order-active"
        fake_db.set(order_id, self._make_order_bytes())

        def _blocked(_order_id):
            self.order_app.abort(409, "Checkout already in progress")

        with mock.patch.object(self.order_app, "db", fake_db), \
             mock.patch.object(
                 self.order_app,
                 "_send_get",
                 return_value=_FakeResponse(200, {"price": 5}),
             ), \
             mock.patch.object(self.order_app, "_acquire_mutation_guard", side_effect=_blocked), \
             mock.patch.object(self.order_app, "_release_mutation_guard"):
            with self.order_app.app.test_client() as client:
                response = client.post(f"/addItem/{order_id}/item-1/1")

        self.assertEqual(response.status_code, 409)
        saved = msgpack.decode(fake_db.get(order_id), type=self.store_module.OrderValue)
        self.assertEqual(saved.items, [])
        self.assertEqual(saved.total_cost, 0)

    def test_checkout_proxy_passthrough_keeps_single_winner_shape(self):
        statuses = []
        start_barrier = threading.Barrier(8)
        state_lock = threading.Lock()
        state = {"winner_assigned": False}

        def _fake_checkout_post(_url, **kwargs):
            with state_lock:
                if not state["winner_assigned"]:
                    state["winner_assigned"] = True
                    return _FakeResponse(200, content=b"Checkout successful")
            return _FakeResponse(409, content=b"Checkout already in progress")

        def _run_checkout():
            start_barrier.wait(timeout=2)
            with self.order_app.app.test_client() as client:
                response = client.post("/checkout/order-1")
                statuses.append(response.status_code)

        with mock.patch.object(self.order_app, "_send_post", side_effect=_fake_checkout_post):
            threads = [threading.Thread(target=_run_checkout) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 7)


if __name__ == "__main__":
    unittest.main()
