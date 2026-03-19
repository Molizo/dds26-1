import threading

import redis


class FakeWatchPipeline:
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


class FakeWatchRedis:
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
        return FakeWatchPipeline(self)

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
