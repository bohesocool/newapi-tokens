from app.cache_utils import TTLResponseCache
import unittest


class TTLResponseCacheTests(unittest.TestCase):
    def test_cache_reuses_value_until_ttl_expires(self):
        calls = []
        now = [100.0]
        cache = TTLResponseCache(clock=lambda: now[0])

        def producer():
            calls.append(1)
            return {"value": len(calls)}

        self.assertEqual(cache.get("k", 10, producer), {"value": 1})
        self.assertEqual(cache.get("k", 10, producer), {"value": 1})

        now[0] = 111.0

        self.assertEqual(cache.get("k", 10, producer), {"value": 2})
        self.assertEqual(len(calls), 2)

    def test_cache_can_invalidate_prefix(self):
        cache = TTLResponseCache(clock=lambda: 1.0)
        cache.get("hourly", 10, lambda: 1)
        cache.get("chstatus:60", 10, lambda: 2)

        cache.invalidate_prefix("chstatus:")

        self.assertEqual(cache.get("hourly", 10, lambda: 3), 1)
        self.assertEqual(cache.get("chstatus:60", 10, lambda: 4), 4)


if __name__ == "__main__":
    unittest.main()
