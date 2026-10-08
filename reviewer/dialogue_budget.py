"""Per-MR daily budget for dialogue replies (DIALOGUE_MAX_REPLIES_PER_MR).

A runaway thread (two bots, a dev arguing with the reviewer) must not burn
main-tier calls without end. Persisted in state/reviewer.db (table
dialogue_replies) so a deploy does not reset the budget mid-day; fail-open.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from .adapters.storage import Database, as_database

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 86_400  # the budget is per rolling day
FILENAME = "dialogue_replies.json"  # the stage-14 file, imported once at startup


def budget_key(mr_key: tuple) -> str:
    return ":".join(str(part) for part in mr_key)


class DialogueBudget:
    """MR key -> timestamps of replies sent in the last day."""

    def __init__(self, db: Database | str | Path, max_replies: Callable[[], int], *,
                 clock: Callable[[], float] = time.time) -> None:
        self.db = as_database(db)
        # read live: the limit is a setting tests (and config reloads) may change
        self._max_replies = max_replies
        self._clock = clock

    def allows(self, mr_key: tuple) -> bool:
        since = self._clock() - WINDOW_SECONDS
        try:
            rows = self.db.query(
                "SELECT COUNT(*) FROM dialogue_replies WHERE mr_key = ? AND ts > ?",
                (budget_key(mr_key), since))
        except sqlite3.Error as exc:
            logger.warning("dialogue budget not readable (%s) — allowing", exc)
            return True
        return rows[0][0] < self._max_replies()

    def record(self, mr_key: tuple) -> None:
        now = self._clock()
        try:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO dialogue_replies (mr_key, ts) VALUES (?, ?)",
                             (budget_key(mr_key), now))
                conn.execute("DELETE FROM dialogue_replies WHERE ts <= ?",
                             (now - WINDOW_SECONDS,))
        except sqlite3.Error as exc:
            logger.warning("dialogue reply not recorded (%s)", exc)
