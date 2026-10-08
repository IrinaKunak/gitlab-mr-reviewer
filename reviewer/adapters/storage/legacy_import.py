"""One-time import of the pre-SQLite state into state/reviewer.db.

Runs at startup after `state_layout.migrate` (which first moved even older
files out of cache/). Each source is imported once — a `kv` row
(namespace `imports`) records it, so restarts never duplicate usage rows even
though usage.jsonl keeps being appended in parallel. The JSON files stay on
disk untouched: rolling back to a pre-SQLite image still finds them.
Fail-open: an unreadable or corrupt source is skipped with a WARNING (and
marked, so the warning is not repeated every start).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .sqlite import Database

logger = logging.getLogger(__name__)

IMPORTS_NS = "imports"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("legacy state %s not imported (%s)", path, exc)
        return None


def _reviewed_shas(db: Database, path: Path) -> int:
    data = _read_json(path)
    if not isinstance(data, dict):
        return 0
    with db.transaction() as conn:
        # file order is oldest-first (re-inserted on update): keep it as seq
        for seq, (key, sha) in enumerate(data.items(), start=1):
            conn.execute("INSERT OR IGNORE INTO reviewed_shas (mr_key, sha, seq) "
                         "VALUES (?, ?, ?)", (str(key), str(sha), seq))
    return len(data)


def _overrides(db: Database, path: Path) -> int:
    data = _read_json(path)
    if not isinstance(data, dict):
        return 0
    if db.kv_get("overrides", "tiers") is None:  # set via the dashboard already: keep
        db.kv_set("overrides", "tiers", json.dumps(data))
    return 1


def _dialogue_replies(db: Database, path: Path) -> int:
    data = _read_json(path)
    if not isinstance(data, dict):
        return 0
    rows = [(str(key), float(ts)) for key, stamps in data.items()
            if isinstance(stamps, list) for ts in stamps]
    with db.transaction() as conn:
        conn.executemany("INSERT INTO dialogue_replies (mr_key, ts) VALUES (?, ?)", rows)
    return len(rows)


def _usage(db: Database, path: Path) -> int:
    from ...usage import UsageLog  # the one place that knows the entry -> row mapping

    sink = UsageLog(path.parent, db)
    count = 0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    sink.insert(entry)
                    count += 1
    except FileNotFoundError:
        return 0
    return count


def import_legacy(db: Database, state_dir: str | Path, log_dir: str | Path) -> dict[str, int]:
    """Import every not-yet-imported legacy source; returns {source: rows}."""
    state, logs = Path(state_dir), Path(log_dir)
    sources = {
        "reviewed_shas.json": (_reviewed_shas, state / "reviewed_shas.json"),
        "model_overrides.json": (_overrides, state / "model_overrides.json"),
        "dialogue_replies.json": (_dialogue_replies, state / "dialogue_replies.json"),
        "usage.jsonl": (_usage, logs / "usage.jsonl"),
    }
    done: dict[str, int] = {}
    for name, (importer, path) in sources.items():
        try:
            if db.kv_get(IMPORTS_NS, name) is not None:
                continue
            done[name] = importer(db, path)
            db.kv_set(IMPORTS_NS, name, json.dumps({"rows": done[name], "path": str(path)}))
            if done[name]:
                logger.info("imported %d row(s) from %s into %s", done[name], path, db.path)
        except (sqlite3.Error, OSError) as exc:
            logger.error("legacy import of %s failed (%s) — will retry next start", path, exc)
    return done
