"""Which changed files the review skips reading (triage's skip_globs)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from fnmatch import fnmatch

from .models import ChangeSet

logger = logging.getLogger(__name__)

MAX_SKIP_GLOBS = 40
MAX_SKIP_SHARE = 0.98  # a pattern set that eats the whole MR is a bad verdict


def resolve_skip(changes: ChangeSet, globs: Iterable[str]) -> set[str]:
    """Expand triage's glob patterns into concrete paths.

    The model picks the rule, this applies it — deterministically, and with
    guards: catch-alls are dropped, and a verdict that would swallow the entire
    MR is discarded so a bad triage can never silence the review."""
    patterns = [g.strip() for g in list(globs)[:MAX_SKIP_GLOBS]
                if isinstance(g, str) and g.strip()
                and g.strip().strip("*/") not in ("", ".")]
    if not patterns:
        return set()
    paths = changes.paths
    matched = {p for p in paths if p and any(
        fnmatch(p, g) or fnmatch(p, g.rstrip("/") + "/*") for g in patterns)}
    if paths and len(matched) > MAX_SKIP_SHARE * len(paths):
        logger.warning("triage skip_globs %s matched %d/%d files — ignoring",
                       patterns, len(matched), len(paths))
        return set()
    return matched
