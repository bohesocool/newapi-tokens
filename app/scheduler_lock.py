import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime


@dataclass
class SQLiteLease:
    db_path: object
    name: str
    owner: str
    acquired: bool

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

    def release(self):
        if not self.acquired:
            return
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "DELETE FROM scheduler_locks WHERE name = ? AND owner = ?",
                (self.name, self.owner),
            )
            conn.commit()
        self.acquired = False


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_locks (
            name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def sqlite_lease(db_path, name, owner=None, ttl_secs=300, now_ts=None):
    owner = owner or f"{os.getpid()}:{id(name)}"
    now = time.time() if now_ts is None else float(now_ts)
    expires_at = now + ttl_secs
    updated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(str(db_path), timeout=5) as conn:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO scheduler_locks (name, owner, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (name, owner, expires_at, updated_at),
        )
        if conn.total_changes:
            conn.commit()
            return SQLiteLease(db_path, name, owner, True)

        cur = conn.execute(
            "UPDATE scheduler_locks SET owner = ?, expires_at = ?, updated_at = ? "
            "WHERE name = ? AND (expires_at <= ? OR owner = ?)",
            (owner, expires_at, updated_at, name, now, owner),
        )
        conn.commit()
        return SQLiteLease(db_path, name, owner, cur.rowcount > 0)
