"""OpenRouter model catalog — ids + live prices for the dashboard.

Lets the dashboard offer ANY OpenRouter model as a tier override (not just the
curated MODEL_PRICES list) and prices unknown models correctly for the cost
stats. The catalog is fetched from the public /models endpoint, cached to the
cache volume with a TTL, and read fail-open: no network / bad response just
means the curated list and $0-for-unknown behavior, never a broken review.

Pricing hot path (usage.cost_usd) only ever READS the cache — it never triggers
a network fetch. Refreshes happen out-of-band from the /admin/models endpoint.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import httpx

from .config import settings

logger = logging.getLogger(__name__)

CATALOG_URL = "https://openrouter.ai/api/v1/models"
TTL_SECONDS = 6 * 3600
_lock = threading.Lock()
_cache: dict[str, tuple[float, float]] | None = None
_fetched_at = 0.0


def _path() -> Path:
    return Path(settings.ai_cache_dir) / "openrouter_models.json"


def _parse(data: dict) -> dict[str, tuple[float, float]]:
    """OpenRouter prices are $/token strings; store $/MTok (input, output)."""
    out: dict[str, tuple[float, float]] = {}
    for model in data.get("data", []):
        mid = model.get("id")
        pricing = model.get("pricing") or {}
        try:
            inp = float(pricing.get("prompt", 0)) * 1_000_000
            outp = float(pricing.get("completion", 0)) * 1_000_000
        except (TypeError, ValueError):
            continue
        # openrouter/auto* router pseudo-models report negative sentinel prices
        # (-1e6) — pricing is per-underlying-model, so skip: they'd corrupt cost
        if mid and inp >= 0 and outp >= 0:
            out[mid] = (inp, outp)
    return out


def _load_disk() -> tuple[dict[str, tuple[float, float]], float]:
    try:
        blob = json.loads(_path().read_text(encoding="utf-8"))
        prices = {k: (float(v[0]), float(v[1])) for k, v in blob.get("prices", {}).items()}
        return prices, float(blob.get("fetched_at", 0))
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return {}, 0.0


def catalog() -> dict[str, tuple[float, float]]:
    """Cached catalog (memory → disk). Read-only; never hits the network."""
    global _cache, _fetched_at
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache, _fetched_at = _load_disk()
    return dict(_cache)


def price_for(model: str) -> tuple[float, float] | None:
    """(input, output) $/MTok for an OpenRouter model id, or None if unknown."""
    return catalog().get(model)


def _fetch_now() -> dict[str, tuple[float, float]]:
    proxy = settings.proxy_url
    with httpx.Client(timeout=15, proxy=proxy) as client:
        resp = client.get(CATALOG_URL)
        resp.raise_for_status()
        return _parse(resp.json())


def refresh(force: bool = False) -> dict[str, tuple[float, float]]:
    """Fetch the catalog if stale (or forced). Fail-open to the cached copy.
    Call from request handlers (async → to_thread), NOT the pricing hot path."""
    global _cache, _fetched_at
    now = time.time()
    current = catalog()
    if not force and current and (now - _fetched_at) < TTL_SECONDS:
        return current
    try:
        prices = _fetch_now()
    except Exception as exc:  # noqa: BLE001 — availability only; never fatal
        logger.warning("OpenRouter model catalog refresh failed (%s) — using cache", exc)
        return current
    with _lock:
        _cache = prices
        _fetched_at = now
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps({"fetched_at": now, "prices": {
                k: [v[0], v[1]] for k, v in prices.items()}}), encoding="utf-8")
        except OSError as exc:
            logger.warning("OpenRouter catalog not persisted (%s)", exc)
    logger.info("OpenRouter model catalog refreshed: %d models", len(prices))
    return prices
