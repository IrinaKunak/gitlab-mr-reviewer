"""Runtime per-tier model overrides, set from the dashboard admin panel.

Defaults come from .env (ANTHROPIC_FAST/MAIN/SMART_MODEL); an override set
here wins until cleared. Persisted to a JSON file on the state volume so it
survives container restarts. A vendor-prefixed override (contains "/", e.g.
openai/gpt-5.6-terra) is routed via OpenRouter by the AI client; plain
claude-* ids keep using the CF gateway.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

TIERS = ("fast", "main", "smart")
_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _path() -> Path:
    return Path(settings.state_dir) / "model_overrides.json"


def load() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            _cache = {t: str(data.get(t) or "").strip() for t in TIERS}
        except (OSError, ValueError):
            _cache = {t: "" for t in TIERS}
    return dict(_cache)


def save(new: dict) -> dict[str, str]:
    """Persist overrides (empty string clears a tier). Fail-open on disk errors."""
    global _cache
    clean = {t: str((new or {}).get(t) or "").strip() for t in TIERS}
    with _lock:
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(clean), encoding="utf-8")
        except OSError as exc:
            logger.warning("model overrides not persisted (%s) — in-memory only", exc)
        _cache = clean
    logger.info("model overrides: %s", {t: v for t, v in clean.items() if v} or "cleared")
    return dict(clean)


def model_for_tier(tier: str, cfg) -> str:
    """Override if set, else the .env default from config."""
    return load().get(tier, "") or cfg.model_for_tier(tier)
