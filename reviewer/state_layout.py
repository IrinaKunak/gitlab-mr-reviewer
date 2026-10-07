"""One-time move of durable state out of the AI response cache dir.

Up to 2026-10 the state files lived next to the AI cache entries in `cache/`,
and the cache's 48h age sweep deleted them: dashboard model overrides silently
reverted on the next restart, and an idle MR lost its last-reviewed SHA (full
re-review instead of incremental). They now live in STATE_DIR; this runs at
startup, is idempotent and fail-open — a failed move only costs what the old
layout was already losing.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .ai_client import CACHE_KEY_RE
from .config import Settings

logger = logging.getLogger(__name__)

STATE_FILES = ("model_overrides.json", "reviewed_shas.json", "openrouter_models.json")


def legacy_dirs(cfg: Settings) -> list[Path]:
    """Where the old layout kept state: the AI cache dir itself (an explicit
    AI_CACHE_DIR kept as-is) and its parent (the default moved cache -> cache/ai)."""
    ai_dir = Path(cfg.ai_cache_dir)
    return [ai_dir, ai_dir.parent]


def migrate(cfg: Settings) -> list[str]:
    """Move legacy state files into STATE_DIR; returns the names moved.

    An existing file in STATE_DIR always wins (never overwritten), so re-runs
    and rollbacks are safe. Stale AI cache entries left in the old cache root
    by the layout change are removed.
    """
    state_dir = Path(cfg.state_dir)
    moved: list[str] = []
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("STATE_DIR %s is not writable (%s) — model overrides and "
                     "review state will not persist; chown it to the container user",
                     state_dir, exc)
        return moved
    for name in STATE_FILES:
        target = state_dir / name
        for src_dir in legacy_dirs(cfg):
            src = src_dir / name
            try:
                if src.resolve() == target.resolve() or not src.is_file():
                    continue
                if target.exists():
                    logger.info("state %s already in %s — leaving legacy copy %s",
                                name, state_dir, src)
                    break
                shutil.move(str(src), str(target))  # copy+delete across bind mounts
                moved.append(name)
                logger.info("moved state %s -> %s", src, target)
                break
            except OSError as exc:
                logger.error("could not move state %s to %s: %s", src, target, exc)
    _drop_orphaned_cache_entries(cfg)
    return moved


def _drop_orphaned_cache_entries(cfg: Settings) -> None:
    """The cache moved from `cache/` to `cache/ai/`; entries in the old root are
    never read again and nothing else would sweep them."""
    old_root = Path(cfg.ai_cache_dir).parent
    if old_root.resolve() == Path(cfg.ai_cache_dir).resolve():
        return
    try:
        for entry in old_root.iterdir():
            if entry.is_file() and CACHE_KEY_RE.fullmatch(entry.name):
                entry.unlink(missing_ok=True)
    except OSError:
        pass
