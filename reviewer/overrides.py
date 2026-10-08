"""Runtime per-tier model overrides, set from the dashboard admin panel.

Defaults come from .env (ANTHROPIC_FAST/MAIN/SMART_MODEL); an override set
here wins until cleared. Persisted in state/reviewer.db (kv namespace
`overrides`) so it survives container restarts. A vendor-prefixed override
(contains "/", e.g. openai/gpt-5.6-terra) is routed via OpenRouter by the AI
client; plain claude-* ids keep using the CF gateway.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .adapters.storage import Database, as_database
from .config import Settings
from .domain.models import Tier

logger = logging.getLogger(__name__)

TIERS = tuple(Tier)
FILENAME = "model_overrides.json"  # the pre-SQLite file, imported once at startup
KV_NAMESPACE, KV_KEY = "overrides", "tiers"


def clean_overrides(data) -> dict[str, str]:
    data = data if isinstance(data, dict) else {}
    return {t.value: str(data.get(t) or "").strip() for t in TIERS}


class ModelOverrides:
    """Overrides on top of the configured tier models."""

    def __init__(self, db: Database | str | Path, cfg: Settings) -> None:
        self.cfg = cfg
        self.db = as_database(db)
        self._memory: dict[str, str] | None = None  # when the db write failed

    def load(self) -> dict[str, str]:
        if self._memory is not None:
            return dict(self._memory)
        try:
            raw = self.db.kv_get(KV_NAMESPACE, KV_KEY)
            return clean_overrides(json.loads(raw) if raw else {})
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("model overrides not readable (%s) — using defaults", exc)
            return clean_overrides({})

    def save(self, new: dict) -> dict[str, str]:
        """Persist overrides (empty string clears a tier). Fail-open on db errors."""
        clean = clean_overrides(new)
        try:
            self.db.kv_set(KV_NAMESPACE, KV_KEY, json.dumps(clean))
            self._memory = None
        except sqlite3.Error as exc:
            logger.warning("model overrides not persisted (%s) — in-memory only", exc)
            self._memory = clean
        logger.info("model overrides: %s", {t: v for t, v in clean.items() if v} or "cleared")
        return dict(clean)

    def model_for_tier(self, tier: Tier) -> str:
        """Override if set, else the configured default."""
        return self.load().get(tier, "") or self.cfg.model_for_tier(tier)
