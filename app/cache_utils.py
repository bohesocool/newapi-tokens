import threading
import time


class TTLResponseCache:
    def __init__(self, clock=None):
        self._clock = clock or time.time
        self._items = {}
        self._lock = threading.RLock()

    def get(self, key, ttl_secs, producer):
        now = self._clock()
        with self._lock:
            hit = self._items.get(key)
            if hit and hit[0] > now:
                return hit[1]

        value = producer()
        expires_at = self._clock() + ttl_secs
        with self._lock:
            self._items[key] = (expires_at, value)
        return value

    def invalidate_prefix(self, prefix):
        with self._lock:
            for key in list(self._items):
                if str(key).startswith(prefix):
                    del self._items[key]
