"""OpenRouter model catalog — ids + live prices for the dashboard.

Lets the dashboard offer ANY OpenRouter model as a tier override (not just the
curated MODEL_PRICES list) and prices unknown models correctly for the cost
stats. The catalog is fetched from the public /models endpoint, cached to the
state volume with a TTL, and read fail-open: no network / bad response just
means the curated list and $0-for-unknown behavior, never a broken review.

Pricing hot path (usage.cost_usd) only ever READS the cache — it never triggers
a network fetch. Refreshes happen out-of-band from the /admin/models endpoint.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from .config import settings
from .json_store import JsonStore

logger = logging.getLogger(__name__)

CATALOG_URL = "https://openrouter.ai/api/v1/models"
TTL_SECONDS = 6 * 3600

Prices = dict[str, tuple[float, float]]


def _parse(data: dict) -> Prices:
    """OpenRouter prices are $/token strings; store $/MTok (input, output)."""
    out: Prices = {}
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


def _from_disk(blob) -> tuple[Prices, float]:
    prices = {k: (float(v[0]), float(v[1])) for k, v in blob.get("prices", {}).items()}
    return prices, float(blob.get("fetched_at", 0))


def _to_disk(value: tuple[Prices, float]) -> dict:
    prices, fetched_at = value
    return {"fetched_at": fetched_at, "prices": {k: [v[0], v[1]] for k, v in prices.items()}}


# (prices, fetched_at) — memory copy of the disk cache
_store: JsonStore[tuple[Prices, float]] = JsonStore(
    lambda: Path(settings.storage.state_dir) / "openrouter_models.json",
    parse=_from_disk, dump=_to_disk, empty=lambda: ({}, 0.0),
    label="OpenRouter catalog")


def catalog() -> Prices:
    """Cached catalog (memory → disk). Read-only; never hits the network."""
    return dict(_store.read()[0])


def price_for(model: str) -> tuple[float, float] | None:
    """(input, output) $/MTok for an OpenRouter model id, or None if unknown."""
    return _store.read()[0].get(model)


def _fetch_now() -> Prices:
    proxy = settings.network.proxy_url
    with httpx.Client(timeout=15, proxy=proxy) as client:
        resp = client.get(CATALOG_URL)
        resp.raise_for_status()
        return _parse(resp.json())


def refresh(force: bool = False) -> Prices:
    """Fetch the catalog if stale (or forced). Fail-open to the cached copy.
    Call from request handlers (async → to_thread), NOT the pricing hot path."""
    now = time.time()
    current, fetched_at = _store.read()
    if not force and current and (now - fetched_at) < TTL_SECONDS:
        return dict(current)
    try:
        prices = _fetch_now()
    except Exception as exc:  # noqa: BLE001 — availability only; never fatal
        logger.warning("OpenRouter model catalog refresh failed (%s) — using cache", exc)
        return dict(current)
    _store.write((prices, now))
    logger.info("OpenRouter model catalog refreshed: %d models", len(prices))
    return dict(prices)
