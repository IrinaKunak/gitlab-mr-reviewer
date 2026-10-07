"""Small JSON state file: lazy read, in-memory copy, atomic fail-open writes.

One implementation for the state files that used to copy the same
_lock/_cache/_path pattern (review_state, overrides, openrouter_models).
Writes go to a temp file in the same directory and are swapped in with
os.replace, so a crash or a full disk mid-write leaves the previous file
intact instead of a truncated one. Everything is fail-open: an unreadable or
corrupt file reads as empty, an unwritable one keeps the value in memory only
— state is an optimisation here, never a reason to fail a review.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# what a malformed-but-valid-JSON file can raise inside a parse function
PARSE_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError)


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via temp file + os.replace; raises OSError.

    The temp file is `<name>.<random>.tmp` in the same directory (os.replace
    is only atomic within one filesystem — the state dir is a bind mount)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; keep write_text's old mode
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class JsonStore(Generic[T]):
    """A JSON file holding one value of type T.

    `path` may be a callable so the location follows live settings
    (STATE_DIR is resolved on use, not at import); when it resolves to a
    different file the in-memory copy is dropped and the new file is read.
    `parse` turns loaded JSON into T (and may raise on bad shape — that reads
    as `empty()`), `dump` turns T back into JSON-serialisable data.
    Callers must treat values returned by `read()` as read-only.
    """

    def __init__(self, path: Path | Callable[[], Path], *, parse: Callable[[Any], T],
                 empty: Callable[[], T], dump: Callable[[T], Any] = lambda v: v,
                 label: str = "state") -> None:
        self._path_fn = path if callable(path) else (lambda: path)
        self._parse = parse
        self._empty = empty
        self._dump = dump
        self._label = label
        self._lock = threading.Lock()
        self._value: T | None = None
        self._loaded_from: Path | None = None

    @property
    def path(self) -> Path:
        return Path(self._path_fn())

    def _current(self) -> T:
        """Value for the current path; caller holds the lock."""
        path = self.path
        if self._value is None or self._loaded_from != path:
            self._value = self._load(path)
            self._loaded_from = path
        return self._value

    def _load(self, path: Path) -> T:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty()
        except OSError as exc:
            logger.warning("%s not readable (%s) — starting empty", self._label, exc)
            return self._empty()
        try:
            return self._parse(json.loads(raw))
        except PARSE_ERRORS as exc:
            logger.warning("%s file %s is corrupt (%s) — starting empty",
                           self._label, path, exc)
            return self._empty()

    def read(self) -> T:
        with self._lock:
            return self._current()

    def update(self, fn: Callable[[T], T]) -> T:
        """Replace the value with fn(current) and persist it atomically.

        fn must return a new object (not mutate its argument). A failed write
        keeps the new value in memory and the old file on disk."""
        with self._lock:
            new = fn(self._current())
            path = self.path
            try:
                atomic_write_text(path, json.dumps(self._dump(new)))
            except OSError as exc:
                logger.warning("%s not persisted (%s) — in-memory only", self._label, exc)
            self._value, self._loaded_from = new, path
            return new

    def write(self, value: T) -> T:
        return self.update(lambda _old: value)

    def invalidate(self) -> None:
        """Forget the in-memory copy; the next read goes to disk (cold start)."""
        with self._lock:
            self._value = None
            self._loaded_from = None
