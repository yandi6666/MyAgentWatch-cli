"""Persistent retry queue for myagentwatch-cli daemon reports."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable


def _backoff_delay(retry_count: int) -> float:
    return min(2 ** max(retry_count - 1, 0), 512)


class RetryQueue:
    def __init__(self, db_path: str, max_items: int = 10000, max_retries: int = 10):
        self.db_path = db_path
        self.max_items = max_items
        self.max_retries = max_retries
        self._memory_conn = None
        if db_path != ":memory:":
            Path(os.path.dirname(db_path)).mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        if self.db_path == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", timeout=10)
                self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS retry_queue (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   endpoint TEXT NOT NULL,
                   payload_json TEXT NOT NULL,
                   created_at REAL NOT NULL,
                   retry_count INTEGER DEFAULT 0,
                   next_retry_at REAL NOT NULL,
                   status TEXT DEFAULT 'pending',
                   last_error TEXT DEFAULT '',
                   last_failed_at REAL
                )"""
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(retry_queue)").fetchall()
            }
            if "last_error" not in columns:
                conn.execute("ALTER TABLE retry_queue ADD COLUMN last_error TEXT DEFAULT ''")
            if "last_failed_at" not in columns:
                conn.execute("ALTER TABLE retry_queue ADD COLUMN last_failed_at REAL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_retry_next "
                "ON retry_queue(status, next_retry_at)"
            )
            conn.commit()

    def enqueue(self, endpoint: str, payload: dict, last_error: str = "") -> int:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO retry_queue
                   (endpoint, payload_json, created_at, retry_count, next_retry_at, status, last_error, last_failed_at)
                   VALUES (?, ?, ?, 0, ?, 'pending', ?, ?)""",
                (
                    endpoint,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                    last_error,
                    now if last_error else None,
                ),
            )
            row_id = cur.lastrowid
            self._trim_pending(conn)
            conn.commit()
            return row_id

    def consume(self, post_func: Callable[[str, dict], dict], max_items: int = 20, on_event: Callable[[str, dict], None] | None = None) -> int:
        now = time.time()
        consumed = 0
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM retry_queue
                   WHERE status = 'pending' AND next_retry_at <= ?
                   ORDER BY id ASC LIMIT ?""",
                (now, max_items),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                try:
                    resp = post_func(row["endpoint"], payload)
                    if isinstance(resp, dict) and "error" in resp:
                        raise RuntimeError(str(resp))
                except Exception as exc:
                    next_count = int(row["retry_count"] or 0) + 1
                    error_text = str(exc)
                    event = {
                        "id": row["id"],
                        "endpoint": row["endpoint"],
                        "retry_count": next_count,
                        "error": error_text,
                    }
                    if next_count >= self.max_retries:
                        conn.execute(
                            """UPDATE retry_queue
                               SET retry_count = ?, status = 'dead',
                                   last_error = ?, last_failed_at = ?
                               WHERE id = ?""",
                            (next_count, error_text, time.time(), row["id"]),
                        )
                        if on_event:
                            on_event("dead", event)
                    else:
                        next_retry = time.time() + _backoff_delay(next_count)
                        conn.execute(
                            """UPDATE retry_queue
                               SET retry_count = ?, next_retry_at = ?,
                                   last_error = ?, last_failed_at = ?
                               WHERE id = ?""",
                            (next_count, next_retry, error_text, time.time(), row["id"]),
                        )
                    continue

                conn.execute("DELETE FROM retry_queue WHERE id = ?", (row["id"],))
                consumed += 1
                if on_event:
                    on_event("success", {
                        "id": row["id"],
                        "endpoint": row["endpoint"],
                        "retry_count": int(row["retry_count"] or 0),
                    })
            conn.commit()
        return consumed

    def stats(self) -> dict:
        now = time.time()
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM retry_queue WHERE status = 'pending'"
            ).fetchone()[0]
            dead = conn.execute(
                "SELECT COUNT(*) FROM retry_queue WHERE status = 'dead'"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM retry_queue").fetchone()[0]
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM retry_queue WHERE status = 'pending'"
            ).fetchone()[0]
            newest = conn.execute(
                "SELECT MAX(created_at) FROM retry_queue WHERE status = 'pending'"
            ).fetchone()[0]
            next_retry = conn.execute(
                "SELECT MIN(next_retry_at) FROM retry_queue WHERE status = 'pending'"
            ).fetchone()[0]
            latest_failure = conn.execute(
                "SELECT MAX(last_failed_at) FROM retry_queue"
            ).fetchone()[0]
            last_error_row = conn.execute(
                """SELECT last_error FROM retry_queue
                   WHERE COALESCE(last_error, '') != ''
                   ORDER BY COALESCE(last_failed_at, created_at) DESC
                   LIMIT 1"""
            ).fetchone()
        return {
            "pending": pending,
            "dead": dead,
            "total_queued": total,
            "oldest_pending_at": oldest,
            "newest_pending_at": newest,
            "next_retry_at": next_retry,
            "oldest_pending_age": round(now - oldest, 1) if oldest else 0,
            "latest_failure_at": latest_failure,
            "last_error": last_error_row[0] if last_error_row else "",
        }

    def details(self, limit: int = 5) -> dict:
        fields = (
            "id, endpoint, created_at, retry_count, next_retry_at, "
            "status, COALESCE(last_error, '') AS last_error, last_failed_at"
        )
        with self._connect() as conn:
            pending = conn.execute(
                f"""SELECT {fields} FROM retry_queue
                    WHERE status = 'pending'
                    ORDER BY id ASC LIMIT ?""",
                (limit,),
            ).fetchall()
            dead = conn.execute(
                f"""SELECT {fields} FROM retry_queue
                    WHERE status = 'dead'
                    ORDER BY COALESCE(last_failed_at, created_at) DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return {
            "stats": self.stats(),
            "pending": [dict(row) for row in pending],
            "dead": [dict(row) for row in dead],
        }

    def cleanup_dead(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM retry_queue WHERE status = 'dead'")
            conn.commit()
            return cur.rowcount

    def _trim_pending(self, conn):
        count = conn.execute(
            "SELECT COUNT(*) FROM retry_queue WHERE status = 'pending'"
        ).fetchone()[0]
        extra = count - self.max_items
        if extra <= 0:
            return
        conn.execute(
            """DELETE FROM retry_queue WHERE id IN (
               SELECT id FROM retry_queue
               WHERE status = 'pending'
               ORDER BY id ASC LIMIT ?
            )""",
            (extra,),
        )
