"""Last-reviewed SHA per MR — enables incremental re-reviews.

On the first review of an MR the whole diff is reviewed; on subsequent pushes
only the delta since the last reviewed SHA is (developer feedback 2026-07-23:
full re-reviews rehashed remarks about earlier commits on every push).
Persisted to the state volume; bounded; fail-open — losing state just means
the next review is a full one.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

MAX_ENTRIES = 500  # oldest-inserted dropped beyond this
_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _path() -> Path:
    return Path(settings.state_dir) / "reviewed_shas.json"


def _key(instance: str, project_id, mr_iid) -> str:
    return f"{instance}:{project_id}:{mr_iid}"


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            _cache = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def get_last_sha(instance: str, project_id, mr_iid) -> str | None:
    with _lock:
        return _load().get(_key(instance, project_id, mr_iid)) or None


def set_last_sha(instance: str, project_id, mr_iid, sha: str) -> None:
    global _cache
    if not sha:
        return
    with _lock:
        state = dict(_load())
        state.pop(_key(instance, project_id, mr_iid), None)  # re-insert as newest
        state[_key(instance, project_id, mr_iid)] = sha
        while len(state) > MAX_ENTRIES:
            state.pop(next(iter(state)))
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(state), encoding="utf-8")
        except OSError as exc:
            logger.warning("review state not persisted (%s) — in-memory only", exc)
        _cache = state
