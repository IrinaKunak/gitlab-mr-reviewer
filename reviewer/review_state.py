"""Last-reviewed SHA per MR — enables incremental re-reviews.

On the first review of an MR the whole diff is reviewed; on subsequent pushes
only the delta since the last reviewed SHA is (developer feedback 2026-07-23:
full re-reviews rehashed remarks about earlier commits on every push).
Persisted in state/reviewer.db (table reviewed_shas); bounded; fail-open —
losing state just means the next review is a full one.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .adapters.storage import Database, as_database

logger = logging.getLogger(__name__)

MAX_ENTRIES = 500  # least recently written dropped beyond this
FILENAME = "reviewed_shas.json"  # the pre-SQLite file, imported once at startup


def mr_key(instance: str, project_id, mr_iid) -> str:
    return f"{instance}:{project_id}:{mr_iid}"


class ReviewStateStore:
    """(instance, project, iid) -> last reviewed sha."""

    def __init__(self, db: Database | str | Path, max_entries: int = MAX_ENTRIES) -> None:
        self.db = as_database(db)
        self.max_entries = max_entries

    def get_last_sha(self, instance: str, project_id, mr_iid) -> str | None:
        try:
            rows = self.db.query("SELECT sha FROM reviewed_shas WHERE mr_key = ?",
                                 (mr_key(instance, project_id, mr_iid),))
        except sqlite3.Error as exc:
            logger.warning("review state not readable (%s) — full review", exc)
            return None
        return (rows[0][0] or None) if rows else None

    def set_last_sha(self, instance: str, project_id, mr_iid, sha: str) -> None:
        if not sha:
            return
        self.put(mr_key(instance, project_id, mr_iid), sha)

    def put(self, key: str, sha: str) -> None:
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO reviewed_shas (mr_key, sha, seq) VALUES "
                    "(?, ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM reviewed_shas)) "
                    "ON CONFLICT (mr_key) DO UPDATE SET sha = excluded.sha, "
                    "seq = excluded.seq", (key, sha))
                conn.execute(
                    "DELETE FROM reviewed_shas WHERE mr_key NOT IN (SELECT mr_key FROM "
                    "reviewed_shas ORDER BY seq DESC LIMIT ?)", (self.max_entries,))
        except sqlite3.Error as exc:
            logger.warning("review state not persisted (%s)", exc)
