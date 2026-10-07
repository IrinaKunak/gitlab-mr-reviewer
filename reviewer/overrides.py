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

from .config import settings
from .json_store import JsonStore

logger = logging.getLogger(__name__)

TIERS = ("fast", "main", "smart")


def _clean(data) -> dict[str, str]:
    return {t: str((data or {}).get(t) or "").strip() for t in TIERS}


_store: JsonStore[dict[str, str]] = JsonStore(
    lambda: Path(settings.state_dir) / "model_overrides.json",
    parse=_clean, empty=lambda: _clean({}), label="model overrides")


def load() -> dict[str, str]:
    return dict(_store.read())


def save(new: dict) -> dict[str, str]:
    """Persist overrides (empty string clears a tier). Fail-open on disk errors."""
    clean = _store.write(_clean(new))
    logger.info("model overrides: %s", {t: v for t, v in clean.items() if v} or "cleared")
    return dict(clean)


def model_for_tier(tier: str, cfg) -> str:
    """Override if set, else the .env default from config."""
    return load().get(tier, "") or cfg.model_for_tier(tier)
