from app.scheduler_lock import sqlite_lease
import tempfile
import unittest
from pathlib import Path


class SQLiteLeaseTests(unittest.TestCase):
    def test_sqlite_lease_allows_only_one_owner_until_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "monitor.db"

            first = sqlite_lease(db_path, "hourly", owner="a", ttl_secs=30, now_ts=100)
            second = sqlite_lease(db_path, "hourly", owner="b", ttl_secs=30, now_ts=101)

            self.assertTrue(first.acquired)
            self.assertFalse(second.acquired)

            expired = sqlite_lease(db_path, "hourly", owner="b", ttl_secs=30, now_ts=131)

            self.assertTrue(expired.acquired)

    def test_sqlite_lease_release_allows_next_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "monitor.db"

            with sqlite_lease(db_path, "daily", owner="a", ttl_secs=30, now_ts=100) as lease:
                self.assertTrue(lease.acquired)

            next_lease = sqlite_lease(db_path, "daily", owner="b", ttl_secs=30, now_ts=101)

            self.assertTrue(next_lease.acquired)


if __name__ == "__main__":
    unittest.main()
