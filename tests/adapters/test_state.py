"""State files: review state, JsonStore, the cache/state directory split."""

from __future__ import annotations

from reviewer.config import Settings
from tests.factories import (
    make_services,
    make_settings,
)


def test_review_state_roundtrip_and_bound(tmp_path, monkeypatch):
    from reviewer.review_state import ReviewStateStore

    review_state = ReviewStateStore(tmp_path)
    assert review_state.get_last_sha("primary", 1, 2) is None
    review_state.set_last_sha("primary", 1, 2, "abc123")
    assert review_state.get_last_sha("primary", 1, 2) == "abc123"
    review_state.set_last_sha("primary", 1, 2, "def456")  # newer push wins
    assert review_state.get_last_sha("primary", 1, 2) == "def456"

    # survives a cold start (persisted to the cache volume)
    assert ReviewStateStore(tmp_path).get_last_sha("primary", 1, 2) == "def456"

    # bounded: oldest entries evicted beyond MAX_ENTRIES
    review_state = ReviewStateStore(tmp_path, max_entries=3)
    for i in range(5):
        review_state.set_last_sha("primary", 100 + i, 1, f"sha{i}")
    assert review_state.get_last_sha("primary", 100, 1) is None
    assert review_state.get_last_sha("primary", 104, 1) == "sha4"


def test_state_files_live_outside_ai_cache_dir(tmp_path):
    cfg = make_settings(storage__state_dir=str(tmp_path / "state"),
                        storage__ai_cache_dir=str(tmp_path / "cache" / "ai"))
    svc = make_services(cfg)
    assert svc.db.path == str(tmp_path / "state" / "reviewer.db")
    assert svc.overrides.db is svc.db and svc.review_state.db is svc.db
    assert svc.catalog._store.path.parent == tmp_path / "state"
    assert Settings().storage.ai_cache_dir != Settings().storage.state_dir


def test_state_migration_moves_legacy_files(tmp_path):
    from reviewer import state_layout
    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path / "cache" / "ai")
    cfg.storage.state_dir = str(tmp_path / "state")
    legacy = tmp_path / "cache"
    legacy.mkdir()
    (legacy / "model_overrides.json").write_text('{"smart": "openai/x"}', encoding="utf-8")
    (legacy / "reviewed_shas.json").write_text('{"k": "old"}', encoding="utf-8")
    (legacy / ("c" * 64)).write_text("orphaned cache entry", encoding="utf-8")
    (legacy / "notes.txt").write_text("unrelated", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "reviewed_shas.json").write_text('{"k": "new"}', encoding="utf-8")

    moved = state_layout.migrate(cfg)

    assert moved == ["model_overrides.json"]
    assert (state / "model_overrides.json").read_text(encoding="utf-8") == '{"smart": "openai/x"}'
    assert not (legacy / "model_overrides.json").exists()
    # an existing state file always wins — the legacy copy is left alone
    assert (state / "reviewed_shas.json").read_text(encoding="utf-8") == '{"k": "new"}'
    assert (legacy / "reviewed_shas.json").exists()
    assert not (legacy / ("c" * 64)).exists()  # old-root cache entry dropped
    assert (legacy / "notes.txt").exists()
    assert state_layout.migrate(cfg) == []  # idempotent


def test_json_store_corrupt_file_reads_as_empty(tmp_path, caplog):
    # stage 7 (#11): a half-written / hand-edited state file must not break
    # reviews — it reads as empty (full review, no overrides), with a warning
    from reviewer.json_store import JsonStore
    path = tmp_path / "state.json"
    path.write_text('{"a": "x", "b"', encoding="utf-8")  # truncated mid-write
    store = JsonStore(path, parse=dict, empty=dict, label="test state")
    with caplog.at_level("WARNING"):
        assert store.read() == {}
    assert "corrupt" in caplog.text
    # valid JSON of the wrong shape is "corrupt" too, not a crash
    path.write_text("[1, 2]", encoding="utf-8")
    store.invalidate()
    assert store.read() == {}
    # a later write replaces the bad file
    store.write({"a": "y"})
    store.invalidate()
    assert store.read() == {"a": "y"}


def test_json_store_failed_write_keeps_old_file(tmp_path, monkeypatch):
    # stage 7 (#11): write_text truncated the file first, so a crash/full disk
    # mid-write left it broken; now the old file survives and no temp is left
    import json as _json

    from reviewer import json_store
    path = tmp_path / "state.json"
    store = json_store.JsonStore(path, parse=dict, empty=dict)
    store.write({"k": "old"})

    def boom(*args, **kwargs):
        raise OSError("No space left on device")
    monkeypatch.setattr(json_store.os, "fsync", boom)
    assert store.write({"k": "new"}) == {"k": "new"}  # fail-open: kept in memory
    assert store.read() == {"k": "new"}
    assert _json.loads(path.read_text(encoding="utf-8")) == {"k": "old"}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # temp cleaned up


def test_json_store_follows_path_change(tmp_path):
    # path is resolved on use: switching STATE_DIR (tests, future reload) reads
    # the new file instead of serving the old dir's in-memory copy
    from reviewer.json_store import JsonStore
    where = {"dir": tmp_path / "a"}
    store = JsonStore(lambda: where["dir"] / "s.json", parse=dict, empty=dict)
    store.write({"x": "1"})
    where["dir"] = tmp_path / "b"
    assert store.read() == {}
    where["dir"] = tmp_path / "a"
    assert store.read() == {"x": "1"}


def test_review_state_survives_corrupt_file(tmp_path):
    from reviewer.review_state import ReviewStateStore
    (tmp_path / "reviewed_shas.json").write_text("{not json", encoding="utf-8")
    review_state = ReviewStateStore(tmp_path)
    assert review_state.get_last_sha("primary", 1, 2) is None  # -> full review
    review_state.set_last_sha("primary", 1, 2, "abc")
    assert ReviewStateStore(tmp_path).get_last_sha("primary", 1, 2) == "abc"
