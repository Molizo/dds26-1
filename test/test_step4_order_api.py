import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import redis
from msgspec import msgpack

from local_app_loader import load_order_app


_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from common.constants import STATUS_COMPLETED, STATUS_HOLDING


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

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

    def delete(self, key):
        self._commands.append(("delete", key, None))

    def execute(self):
        with self._db._lock:
            for key, watched_revision in self._watched_revisions.items():
                if self._db._revisions.get(key, 0) != watched_revision:
                    raise redis.WatchError()

            for command, key, value in self._commands:
                if command == "set":
                    self._db._set_locked(key, value)
                elif command == "delete":
                    self._db._delete_locked(key)
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

    def delete(self, key):
        with self._lock:
            return self._delete_locked(key)

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

    def _delete_locked(self, key):
        existed = key in self._data
        self._data.pop(key, None)
        self._revisions[key] = self._revisions.get(key, 0) + 1
        return 1 if existed else 0


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

        with patch.object(self.order_app, "db", fake_db), \
             patch.object(self.order_app, "_send_get", return_value=_FakeResponse(200, {"price": 5})):
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
        with patch.object(self.order_app, "_send_get", side_effect=AssertionError("should not call stock")):
            with self.order_app.app.test_client() as client:
                zero_response = client.post("/addItem/order-1/item-1/0")
                negative_response = client.post("/addItem/order-1/item-1/-2")

        self.assertEqual(zero_response.status_code, 400)
        self.assertEqual(negative_response.status_code, 400)

    def test_add_item_rejects_paid_order(self):
        fake_db = _FakeWatchRedis()
        order_id = "order-paid"
        fake_db.set(order_id, self._make_order_bytes(paid=True, items=[("item-1", 1)], total_cost=5))

        with patch.object(self.order_app, "db", fake_db), \
             patch.object(self.order_app, "_send_get", return_value=_FakeResponse(200, {"price": 5})):
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
        fake_db.set(f"order_active_tx:{order_id}", "tx-1")

        with patch.object(self.order_app, "db", fake_db), \
             patch.object(self.order_app, "_send_get", return_value=_FakeResponse(200, {"price": 5})), \
             patch.object(self.order_app, "get_tx", return_value=SimpleNamespace(status=STATUS_HOLDING)):
            with self.order_app.app.test_client() as client:
                response = client.post(f"/addItem/{order_id}/item-1/1")

        self.assertEqual(response.status_code, 409)
        saved = msgpack.decode(fake_db.get(order_id), type=self.store_module.OrderValue)
        self.assertEqual(saved.items, [])
        self.assertEqual(saved.total_cost, 0)

    def test_checkout_conflict_storm_allows_single_winner(self):
        statuses = []
        start_barrier = threading.Barrier(8)
        tx_state = {"guard_tx_id": None, "status": None}
        state_lock = threading.Lock()
        all_attempted = threading.Event()
        attempt_counter = {"count": 0}

        class _BlockingCoordinator:
            def execute_checkout(self, order_id, protocol, tx_id):
                with state_lock:
                    tx_state["status"] = STATUS_HOLDING
                all_attempted.wait(timeout=2)
                with state_lock:
                    tx_state["status"] = STATUS_COMPLETED
                return self.order_app.CheckoutResult.ok()

        coordinator = _BlockingCoordinator()
        coordinator.order_app = self.order_app

        def _acquire_guard(_db, order_id, tx_id, ttl):
            with state_lock:
                attempt_counter["count"] += 1
                if attempt_counter["count"] == 8:
                    all_attempted.set()
                if tx_state["guard_tx_id"] is not None:
                    return False
                tx_state["guard_tx_id"] = tx_id
                tx_state["status"] = STATUS_HOLDING
                return True

        def _get_guard(_db, order_id):
            with state_lock:
                return tx_state["guard_tx_id"]

        def _get_tx(_db, tx_id):
            with state_lock:
                if tx_id != tx_state["guard_tx_id"] or tx_state["status"] is None:
                    return None
                return SimpleNamespace(status=tx_state["status"])

        def _clear_guard(_db, order_id):
            with state_lock:
                tx_state["guard_tx_id"] = None
                tx_state["status"] = None

        def _run_checkout():
            start_barrier.wait(timeout=2)
            with self.order_app.app.test_client() as client:
                response = client.post("/checkout/order-1")
                statuses.append(response.status_code)

        with patch.object(
            self.order_app,
            "_get_order_or_abort",
            return_value=self.store_module.OrderValue(
                paid=False,
                items=[("item-1", 1)],
                user_id="user-1",
                total_cost=5,
            ),
        ), \
             patch.object(self.order_app, "_get_coordinator", return_value=coordinator), \
             patch.object(self.order_app, "acquire_active_tx_guard", side_effect=_acquire_guard), \
             patch.object(self.order_app, "get_active_tx_guard", side_effect=_get_guard), \
             patch.object(self.order_app, "get_tx", side_effect=_get_tx), \
             patch.object(self.order_app, "clear_active_tx_guard", side_effect=_clear_guard):
            threads = [threading.Thread(target=_run_checkout) for _ in range(8)]
            for thread in threads:
                thread.start()

            for thread in threads:
                thread.join()

        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 7)


if __name__ == '__main__':
    unittest.main()
