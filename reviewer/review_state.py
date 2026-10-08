"""Last-reviewed SHA per MR — enables incremental re-reviews.

On the first review of an MR the whole diff is reviewed; on subsequent pushes
only the delta since the last reviewed SHA is (developer feedback 2026-07-23:
full re-reviews rehashed remarks about earlier commits on every push).
Persisted to the state volume; bounded; fail-open — losing state just means
the next review is a full one.
"""

from __future__ import annotations

from pathlib import Path

from .json_store import JsonStore

MAX_ENTRIES = 500  # oldest-inserted dropped beyond this
FILENAME = "reviewed_shas.json"


def _parse(data) -> dict[str, str]:
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _key(instance: str, project_id, mr_iid) -> str:
    return f"{instance}:{project_id}:{mr_iid}"


class ReviewStateStore:
    """state_dir/reviewed_shas.json: (instance, project, iid) -> last reviewed sha."""

    def __init__(self, state_dir: str | Path, max_entries: int = MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._store: JsonStore[dict[str, str]] = JsonStore(
            Path(state_dir) / FILENAME, parse=_parse, empty=dict, label="review state")

    def get_last_sha(self, instance: str, project_id, mr_iid) -> str | None:
        return self._store.read().get(_key(instance, project_id, mr_iid)) or None

    def set_last_sha(self, instance: str, project_id, mr_iid, sha: str) -> None:
        if not sha:
            return
        key = _key(instance, project_id, mr_iid)

        def _put(old: dict[str, str]) -> dict[str, str]:
            state = dict(old)
            state.pop(key, None)  # re-insert as newest
            state[key] = sha
            while len(state) > self.max_entries:
                state.pop(next(iter(state)))
            return state

        self._store.update(_put)
