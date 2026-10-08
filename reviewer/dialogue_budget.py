"""Per-MR daily budget for dialogue replies (DIALOGUE_MAX_REPLIES_PER_MR).

A runaway thread (two bots, a dev arguing with the reviewer) must not burn
main-tier calls without end. Persisted to the state volume so a deploy does
not reset the budget mid-day; bounded; fail-open like every state file.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .json_store import JsonStore

WINDOW_SECONDS = 86_400  # the budget is per rolling day
MAX_KEYS = 500  # MRs tracked; stale ones are pruned first
FILENAME = "dialogue_replies.json"

Stamps = dict[str, list[float]]


def _parse(data) -> Stamps:
    if not isinstance(data, dict):
        return {}
    return {str(k): [float(t) for t in v] for k, v in data.items() if isinstance(v, list)}


def _key(mr_key: tuple) -> str:
    return ":".join(str(part) for part in mr_key)


class DialogueBudget:
    """state_dir/dialogue_replies.json: MR key -> timestamps of replies sent."""

    def __init__(self, state_dir: str | Path, max_replies: Callable[[], int], *,
                 clock: Callable[[], float] = time.time) -> None:
        # read live: the limit is a setting tests (and config reloads) may change
        self._max_replies = max_replies
        self._clock = clock
        self._store: JsonStore[Stamps] = JsonStore(
            Path(state_dir) / FILENAME, parse=_parse, empty=dict, label="dialogue budget")

    def _recent(self, stamps: list[float], now: float) -> list[float]:
        return [t for t in stamps if now - t < WINDOW_SECONDS]

    def allows(self, mr_key: tuple) -> bool:
        now = self._clock()
        recent = self._recent(self._store.read().get(_key(mr_key), []), now)
        return len(recent) < self._max_replies()

    def record(self, mr_key: tuple) -> None:
        now, key = self._clock(), _key(mr_key)

        def _add(old: Stamps) -> Stamps:
            state = {k: v for k, v in ((k, self._recent(v, now)) for k, v in old.items())
                     if v and k != key}
            state[key] = self._recent(old.get(key, []), now) + [now]
            while len(state) > MAX_KEYS:  # oldest-inserted first
                state.pop(next(iter(state)))
            return state

        self._store.update(_add)
