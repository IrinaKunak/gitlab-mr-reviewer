"""state/reviewer.db: one SQLite file for the service's durable state.

Review state (last reviewed sha per MR), dashboard model overrides, the
dialogue reply budget and usage accounting live here (stage 17; the job queue
joins in stage 18). WAL mode: readers (/stats) never block the workers.
Schema changes are numbered migrations tracked in `PRAGMA user_version`.

SQL stays within SQLite 3.40 (the image has 3.46; older distro builds should
still work): JSON1 functions yes, unixepoch('subsec') no.

Fail-open like the JSON files it replaces: a database that cannot be opened
(unwritable STATE_DIR) is replaced by an in-memory one with an ERROR in the
log — the service keeps reviewing, it just forgets on restart.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_FILENAME = "reviewer.db"

# MIGRATIONS[i] brings the schema from version i to i+1. Append only.
MIGRATIONS: list[str] = [
    # 1 — stage 17: state + usage
    """
    CREATE TABLE kv (
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (namespace, key)
    );
    CREATE TABLE reviewed_shas (
        mr_key TEXT PRIMARY KEY,
        sha TEXT NOT NULL,
        seq INTEGER NOT NULL
    );
    CREATE TABLE dialogue_replies (
        mr_key TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE INDEX dialogue_replies_mr ON dialogue_replies (mr_key, ts);
    CREATE TABLE usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        instance TEXT NOT NULL,
        project TEXT NOT NULL,
        mr_iid INTEGER,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        cache_savings_usd REAL NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        entry TEXT NOT NULL
    );
    CREATE INDEX usage_ts ON usage (ts);
    """,
]


class Database:
    """One connection shared by the stores (serialized by a lock; SQLite
    statements here are sub-millisecond, the workers are I/O-bound on AI calls)."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = self._open()

    @classmethod
    def in_dir(cls, state_dir: str | Path) -> Database:
        return cls(Path(state_dir) / DB_FILENAME)

    @classmethod
    def memory(cls) -> Database:
        return cls(":memory:")

    def _connect(self, target: str) -> sqlite3.Connection:
        conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None,
                               timeout=10.0)
        conn.row_factory = sqlite3.Row
        if target != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate(conn)
        return conn

    def _open(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                return self._connect(self.path)
            except (sqlite3.Error, OSError) as exc:
                logger.error("state database %s is unusable (%s) — state is kept in "
                             "memory until restart; chown STATE_DIR to the container user",
                             self.path, exc)
        return self._connect(":memory:")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number in range(version, len(MIGRATIONS)):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in MIGRATIONS[number].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {number + 1}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            logger.info("state database schema -> v%d", number + 1)

    @property
    def schema_version(self) -> int:
        return int(self.query("PRAGMA user_version")[0][0])

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on error) on the shared connection."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # --- kv namespace helpers ---

    def kv_get(self, namespace: str, key: str) -> str | None:
        rows = self.query("SELECT value FROM kv WHERE namespace = ? AND key = ?",
                          (namespace, key))
        return rows[0][0] if rows else None

    def kv_set(self, namespace: str, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv (namespace, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at", (namespace, key, value, time.time()))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def as_database(db_or_dir: Database | str | Path) -> Database:
    """Stores take the shared Database (bootstrap) or a state dir (tests, tools)."""
    return db_or_dir if isinstance(db_or_dir, Database) else Database.in_dir(db_or_dir)
