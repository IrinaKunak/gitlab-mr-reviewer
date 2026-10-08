"""Runtime per-tier model overrides, set from the dashboard admin panel.

Defaults come from .env (ANTHROPIC_FAST/MAIN/SMART_MODEL); an override set
here wins until cleared. Persisted to a JSON file on the state volume so it
survives container restarts. A vendor-prefixed override (contains "/", e.g.
openai/gpt-5.6-terra) is routed via OpenRouter by the AI client; plain
claude-* ids keep using the CF gateway.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Settings
from .domain.models import Tier
from .json_store import JsonStore

logger = logging.getLogger(__name__)

TIERS = tuple(Tier)
FILENAME = "model_overrides.json"


def _clean(data) -> dict[str, str]:
    return {t.value: str((data or {}).get(t) or "").strip() for t in TIERS}


class ModelOverrides:
    """state_dir/model_overrides.json, on top of the configured tier models."""

    def __init__(self, state_dir: str | Path, cfg: Settings) -> None:
        self.cfg = cfg
        self._store: JsonStore[dict[str, str]] = JsonStore(
            Path(state_dir) / FILENAME, parse=_clean, empty=lambda: _clean({}),
            label="model overrides")

    def load(self) -> dict[str, str]:
        return dict(self._store.read())

    def save(self, new: dict) -> dict[str, str]:
        """Persist overrides (empty string clears a tier). Fail-open on disk errors."""
        clean = self._store.write(_clean(new))
        logger.info("model overrides: %s", {t: v for t, v in clean.items() if v} or "cleared")
        return dict(clean)

    def model_for_tier(self, tier: Tier) -> str:
        """Override if set, else the configured default."""
        return self.load().get(tier, "") or self.cfg.model_for_tier(tier)
