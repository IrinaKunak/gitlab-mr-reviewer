"""SQLite state (stage 17): schema migrations, the one-time JSON/JSONL import,
SQL-backed /stats, fail-open on an unusable state dir."""

from __future__ import annotations

import json
import sqlite3

from reviewer.adapters.storage import Database
from reviewer.adapters.storage.legacy_import import import_legacy
from reviewer.adapters.storage.sqlite import MIGRATIONS
from reviewer.dialogue_budget import DialogueBudget
from reviewer.overrides import ModelOverrides
from reviewer.review_state import ReviewStateStore
from reviewer.usage import UsageLog, UsageTracker
from tests.factories import make_settings, review_job


def test_schema_is_migrated_once_and_wal(tmp_path):
    db = Database.in_dir(tmp_path)
    assert db.schema_version == len(MIGRATIONS)
    assert db.query("PRAGMA journal_mode")[0][0] == "wal"
    db.kv_set("x", "y", "1")
    db.close()
    again = Database.in_dir(tmp_path)  # re-open: no migration re-run, data intact
    assert again.schema_version == len(MIGRATIONS) and again.kv_get("x", "y") == "1"


def test_unusable_state_dir_falls_back_to_memory(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    db = Database.in_dir(blocker / "state")  # mkdir fails: a file is in the way
    assert "unusable" in caplog.text
    store = ReviewStateStore(db)
    store.set_last_sha("primary", 1, 2, "abc")
    assert store.get_last_sha("primary", 1, 2) == "abc"  # still works, in memory


def test_legacy_import_is_one_time_and_keeps_files(tmp_path):
    state, logs = tmp_path / "state", tmp_path / "logs"
    state.mkdir()
    logs.mkdir()
    (state / "reviewed_shas.json").write_text(json.dumps({"primary:1:2": "old",
                                                          "primary:1:3": "new"}))
    (state / "model_overrides.json").write_text('{"smart": "openai/gpt-5.6-terra"}')
    (state / "dialogue_replies.json").write_text('{"primary:1:2": [9999999999.0]}')
    entry = {"ts": "2026-10-01T10:00:00Z", "kind": "review", "instance": "primary",
             "project": "g/p", "mr_iid": 2, "input_tokens": 100, "cached_tokens": 0,
             "cache_savings_usd": 0.0, "output_tokens": 10, "cost_usd": 0.5,
             "models": {"claude-sonnet-5": {"calls": 1, "input_tokens": 100,
                                            "output_tokens": 10, "cost_usd": 0.5}}}
    (logs / "usage.jsonl").write_text(json.dumps(entry) + "\n{broken\n")

    db = Database.in_dir(state)
    assert import_legacy(db, state, logs) == {
        "reviewed_shas.json": 2, "model_overrides.json": 1,
        "dialogue_replies.json": 1, "usage.jsonl": 1}
    assert import_legacy(db, state, logs) == {}  # idempotent: nothing twice

    assert ReviewStateStore(db).get_last_sha("primary", 1, 3) == "new"
    assert ModelOverrides(db, make_settings()).load()["smart"] == "openai/gpt-5.6-terra"
    assert DialogueBudget(db, lambda: 1).allows(("primary", 1, 2)) is False
    agg = UsageLog(logs, db).aggregate()
    assert agg["totals"]["reviews"] == 1 and agg["totals"]["cost_usd"] == 0.5
    # the JSON files stay for a rollback to a pre-SQLite image
    assert (state / "reviewed_shas.json").exists() and (logs / "usage.jsonl").exists()


def test_dashboard_override_wins_over_legacy_file(tmp_path):
    db = Database.in_dir(tmp_path)
    ModelOverrides(db, make_settings()).save({"main": "claude-sonnet-5-5"})
    (tmp_path / "model_overrides.json").write_text('{"main": "stale"}')
    import_legacy(db, tmp_path, tmp_path)
    assert ModelOverrides(db, make_settings()).load()["main"] == "claude-sonnet-5-5"


def test_usage_is_written_to_db_and_jsonl(tmp_path):
    db = Database.in_dir(tmp_path)
    log = UsageLog(tmp_path / "logs", db)
    tracker = UsageTracker()
    tracker.record(tier="main", model="claude-sonnet-5", provider="gateway",
                   input_tokens=1000, output_tokens=100, cache_read_tokens=500)
    log.persist(tracker, review_job(mr_iid=9))
    (line,) = (tmp_path / "logs" / "usage.jsonl").read_text().splitlines()
    (row,) = db.query("SELECT mr_iid, input_tokens, cached_tokens, entry FROM usage")
    assert row["mr_iid"] == 9 and row["input_tokens"] == 1500 and row["cached_tokens"] == 500
    assert json.loads(row["entry"]) == json.loads(line)
    agg = log.aggregate()
    assert agg["by_model"]["claude-sonnet-5"]["cache_read_tokens"] == 500
    assert agg["recent"][0]["mr_iid"] == 9


def test_usage_survives_unwritable_logs_dir(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("not a dir")  # logs dir unusable: jsonl lost, db keeps it
    db = Database.in_dir(tmp_path / "state")
    log = UsageLog(blocker, db)
    tracker = UsageTracker()
    tracker.record(tier="fast", model="claude-haiku-4-5", provider="gateway",
                   input_tokens=1, output_tokens=1)
    log.persist(tracker, review_job())
    assert log.aggregate()["totals"]["reviews"] == 1


def test_review_state_write_failure_is_fail_open(tmp_path, monkeypatch):
    db = Database.in_dir(tmp_path)
    store = ReviewStateStore(db)

    def broken():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(db, "transaction", broken)
    store.set_last_sha("primary", 1, 2, "abc")  # logged, not raised
    assert store.get_last_sha("primary", 1, 2) is None  # -> full review next time
