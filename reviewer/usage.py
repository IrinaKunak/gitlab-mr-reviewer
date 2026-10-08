"""Per-review token/cost accounting.

Every AI call records (tier, model, provider, tokens) into a per-review
tracker held in a contextvar; the pipeline persists one JSONL entry per
review through `UsageLog` (logs/usage.jsonl), whose `aggregate()` folds that
file into overall stats for the /stats endpoint. `Pricing` turns tokens into
list-price dollars (curated table + MODEL_PRICES + live OpenRouter catalog).
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .domain.models import Job

logger = logging.getLogger(__name__)

# $ per MTok (input, output). Override/extend via MODEL_PRICES,
# e.g. MODEL_PRICES="claude-sonnet-5=3/15,google/gemini-4-flash=2/8", or
# config.yaml llm.prices
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),      # intro pricing through 2026-08-31
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),        # same price as 4.8, released 2026-07-24
    "claude-fable-5": (10.0, 50.0),
    "anthropic/claude-haiku-4.5": (1.0, 5.0),
    "anthropic/claude-sonnet-5": (2.0, 10.0),
    "anthropic/claude-sonnet-5.5": (2.0, 10.0),
    "anthropic/claude-opus-4.8": (5.0, 25.0),
    "anthropic/claude-opus-5": (5.0, 25.0),
    "google/gemini-3.6-flash": (1.5, 7.5),
    "google/gemini-3.5-flash-lite": (0.3, 2.5),
    "deepseek/deepseek-v4-flash": (0.1, 0.2),
    "deepseek/deepseek-v4-pro": (0.43, 0.87),
    "openai/gpt-5.6-terra": (2.5, 15.0),
    "moonshotai/kimi-k3": (3.0, 15.0),
}


# cache pricing vs the model's input price (Anthropic ratios: reads 0.1x,
# writes 1.25x — close enough for OpenAI/Gemini auto-caching via OpenRouter,
# and keeps the stated "list-price ceiling" stance)
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


class PriceSource(Protocol):
    def price_for(self, model: str) -> tuple[float, float] | None: ...


class Pricing:
    """$/MTok lookup. Curated DEFAULT_PRICES + MODEL_PRICES (config llm.prices)
    win; then the live OpenRouter catalog (so any vendor-prefixed model is
    priced); else $0. The catalog is only ever READ here — never fetched."""

    def __init__(self, overrides: dict[str, tuple[float, float]] | None = None,
                 catalog: PriceSource | None = None) -> None:
        self.prices = {**DEFAULT_PRICES, **(overrides or {})}
        self._catalog = catalog

    def model_key(self, model: str) -> str:
        """Normalize dated model ids (claude-haiku-4-5-20251001 -> claude-haiku-4-5)."""
        if model in self.prices:
            return model
        for known in self.prices:
            if model.startswith(known + "-"):
                return known
        return model

    def price_of(self, model: str) -> tuple[float, float]:
        key = self.model_key(model)
        if key in self.prices:
            return self.prices[key]
        catalog_price = self._catalog.price_for(model) if self._catalog else None
        if catalog_price is not None:
            return catalog_price
        return (0.0, 0.0)

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int,
                 cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> float:
        inp, outp = self.price_of(model)
        return (input_tokens * inp + output_tokens * outp
                + cache_read_tokens * inp * CACHE_READ_MULT
                + cache_creation_tokens * inp * CACHE_WRITE_MULT) / 1_000_000


BUILTIN_PRICING = Pricing()  # curated table only: no config, no catalog


@dataclass
class UsageTracker:
    pricing: Pricing = BUILTIN_PRICING
    calls: list[dict] = field(default_factory=list)
    cache_alerted: bool = False  # one prompt-cache alert per review, not per loop

    def record(self, *, tier: str, model: str, provider: str,
               input_tokens: int, output_tokens: int,
               cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> None:
        # wire-format input_tokens EXCLUDES cached tokens — models with
        # automatic prompt caching (gpt/gemini/deepseek via OpenRouter)
        # report 9-token inputs on 100k prompts; store the full amount the
        # model read, price the cached parts at their discounted rates
        self.calls.append({
            "tier": tier, "model": self.pricing.model_key(model), "provider": provider,
            "input_tokens": input_tokens + cache_read_tokens + cache_creation_tokens,
            "cached_tokens": cache_read_tokens + cache_creation_tokens,
            # kept apart: reads are the saving (0.1x), creations the premium (1.25x)
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "output_tokens": output_tokens,
            "cost_usd": self.pricing.cost_usd(model, input_tokens, output_tokens,
                                              cache_read_tokens, cache_creation_tokens),
        })

    def by_model(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for call in self.calls:
            m = out.setdefault(call["model"], {
                "calls": 0, "input_tokens": 0, "cached_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "output_tokens": 0, "cost_usd": 0.0})
            m["calls"] += 1
            m["input_tokens"] += call["input_tokens"]
            m["cached_tokens"] += call.get("cached_tokens", 0)
            m["cache_read_tokens"] += call.get("cache_read_tokens", 0)
            m["cache_creation_tokens"] += call.get("cache_creation_tokens", 0)
            m["output_tokens"] += call["output_tokens"]
            m["cost_usd"] = round(m["cost_usd"] + call["cost_usd"], 6)
        return out

    def cache_savings(self) -> float:
        """Net $ vs paying full input price for the same tokens: reads save 0.9x,
        cache writes cost a 0.25x premium."""
        saved = 0.0
        for call in self.calls:
            inp, _ = self.pricing.price_of(call["model"])
            saved += (call.get("cache_read_tokens", 0) * inp * (1 - CACHE_READ_MULT)
                      - call.get("cache_creation_tokens", 0) * inp
                      * (CACHE_WRITE_MULT - 1)) / 1_000_000
        return round(saved, 6)

    @property
    def total_cost(self) -> float:
        return round(sum(c["cost_usd"] for c in self.calls), 6)

    @property
    def total_input(self) -> int:
        return sum(c["input_tokens"] for c in self.calls)

    @property
    def total_output(self) -> int:
        return sum(c["output_tokens"] for c in self.calls)

    def summary_line(self) -> str:
        per_model = ", ".join(
            f"{model} ${stats['cost_usd']:.2f}"
            for model, stats in sorted(self.by_model().items()))
        return (f"${self.total_cost:.2f} "
                f"({self.total_input:,}→{self.total_output:,} tok; {per_model})")

    def footer_line(self) -> str:
        """Compact usage footer for Telegram messages (AIManager style)."""
        segs = []
        for model, stats in sorted(self.by_model().items()):
            name = model.split("/")[-1].replace("claude-", "")
            segs.append(f"{name}: →{stats['input_tokens']} ←{stats['output_tokens']}")
        segs.append(f"💰${self.total_cost:.2f}")
        return " | ".join(segs)


current_tracker: contextvars.ContextVar[UsageTracker | None] = contextvars.ContextVar(
    "usage_tracker", default=None)


def record(**kwargs) -> None:
    """Record one AI call into the active review's tracker (no-op outside one)."""
    tracker = current_tracker.get()
    if tracker is not None:
        tracker.record(**kwargs)


class UsageLog:
    """logs/usage.jsonl: one entry per review/dialogue. Fail-open — the logs
    dir can be unwritable (bind-mount ownership); accounting never breaks a review."""

    def __init__(self, log_dir: str | Path) -> None:
        self.path = Path(log_dir) / "usage.jsonl"

    def persist(self, tracker: UsageTracker, job: Job) -> None:
        """Append one per-review entry; never let accounting break a review."""
        if not tracker.calls:
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": str(job.kind),  # review | dialogue
            "instance": job.ref.instance.name,
            "project": job.ref.project_path,
            "mr_iid": job.ref.mr_iid,
            "input_tokens": tracker.total_input,
            "cached_tokens": sum(c.get("cached_tokens", 0) for c in tracker.calls),
            "cache_savings_usd": tracker.cache_savings(),
            "output_tokens": tracker.total_output,
            "cost_usd": tracker.total_cost,
            "models": tracker.by_model(),
        }
        logger.info("usage: %s !%s — %s", entry["project"], entry["mr_iid"],
                    tracker.summary_line())
        try:
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("usage persist failed (%s) — stats entry lost", exc)


    def aggregate(self) -> dict:
        """Overall stats from usage.jsonl for the /stats endpoint and dashboard."""
        totals = {"reviews": 0, "input_tokens": 0, "cached_tokens": 0,
                  "output_tokens": 0, "cost_usd": 0.0, "cache_savings_usd": 0.0}
        models: dict[str, dict] = {}
        daily: dict[str, dict] = {}
        recent: list[dict] = []
        try:
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    totals["reviews"] += 1
                    totals["input_tokens"] += entry.get("input_tokens", 0)
                    totals["cached_tokens"] += entry.get("cached_tokens", 0)
                    totals["output_tokens"] += entry.get("output_tokens", 0)
                    totals["cost_usd"] = round(
                        totals["cost_usd"] + entry.get("cost_usd", 0.0), 6)
                    totals["cache_savings_usd"] = round(
                        totals["cache_savings_usd"]
                        + entry.get("cache_savings_usd", 0.0), 6)
                    for model, stats in (entry.get("models") or {}).items():
                        m = models.setdefault(model, {
                            "calls": 0, "input_tokens": 0, "cached_tokens": 0,
                            "cache_read_tokens": 0, "cache_creation_tokens": 0,
                            "output_tokens": 0, "cost_usd": 0.0})
                        m["calls"] += stats.get("calls", 0)
                        m["input_tokens"] += stats.get("input_tokens", 0)
                        m["cached_tokens"] += stats.get("cached_tokens", 0)
                        m["cache_read_tokens"] += stats.get("cache_read_tokens", 0)
                        m["cache_creation_tokens"] += stats.get(
                            "cache_creation_tokens", 0)
                        m["output_tokens"] += stats.get("output_tokens", 0)
                        m["cost_usd"] = round(
                            m["cost_usd"] + stats.get("cost_usd", 0.0), 6)
                    day = (entry.get("ts") or "")[:10]
                    if day:
                        d = daily.setdefault(day, {"reviews": 0, "cost_usd": 0.0})
                        d["reviews"] += 1
                        d["cost_usd"] = round(
                            d["cost_usd"] + entry.get("cost_usd", 0.0), 6)
                    recent.append(entry)
        except FileNotFoundError:
            pass
        return {"totals": totals, "by_model": models, "daily": daily,
                "recent": recent[-20:]}
