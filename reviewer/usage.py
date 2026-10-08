"""Per-review token/cost accounting.

Every AI call records (tier, model, provider, tokens) into a per-review
tracker carried by the job's logging_setup.JobContext; the use cases persist one entry per review
through `UsageLog` (the `usage` table in state/reviewer.db + logs/usage.jsonl),
whose `aggregate()` is the SQL behind the /stats endpoint. `Pricing` turns tokens into
list-price dollars (curated table + MODEL_PRICES + live OpenRouter catalog).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .adapters.storage import Database
from .config import DEFAULT_MODELS, match_model_key
from .domain.events import ModelUsage, UsageSummary
from .domain.models import Job
from .logging_setup import current_job

logger = logging.getLogger(__name__)

# $ per MTok (input, output): the `price` column of the model table in config
# (DEFAULT_MODELS); override/extend via MODEL_PRICES="claude-sonnet-5=3/15,..."
# or config.yaml llm.prices / llm.models
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    key: spec["price"] for key, spec in DEFAULT_MODELS.items() if spec.get("price")}


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
        return match_model_key(model, self.prices) or model

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

    def summary(self) -> UsageSummary:
        """What a notification carries (the channel formats its own footer)."""
        return UsageSummary(
            models=tuple(ModelUsage(model, stats["input_tokens"], stats["output_tokens"],
                                    stats["cost_usd"])
                         for model, stats in sorted(self.by_model().items())),
            total_cost_usd=self.total_cost)


@dataclass
class UsageAccumulator:
    """Token totals across the turns of one agent loop. Prompt-cache tokens are
    reported SEPARATELY from input_tokens on the wire (auto-caching models via
    OpenRouter put nearly the whole prompt there), so they are kept apart."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, usage_info: object) -> None:
        """Fold in one response's `usage` (absent fields count as 0)."""
        self.input_tokens += getattr(usage_info, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage_info, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage_info, "cache_read_input_tokens", 0) or 0
        self.cache_creation_tokens += getattr(usage_info, "cache_creation_input_tokens", 0) or 0

    def __bool__(self) -> bool:
        return bool(self.input_tokens or self.output_tokens
                    or self.cache_read_tokens or self.cache_creation_tokens)

    def record(self, *, tier: str, model: str, provider: str) -> None:
        """Into the active review's tracker — once per loop, on EVERY exit path:
        a failed investigation's completed turns are still real spend."""
        if self:
            record(tier=tier, model=model, provider=provider,
                   input_tokens=self.input_tokens, output_tokens=self.output_tokens,
                   cache_read_tokens=self.cache_read_tokens,
                   cache_creation_tokens=self.cache_creation_tokens)


def current_tracker() -> UsageTracker | None:
    """The running job's tracker (logging_setup.JobContext), None outside a job."""
    ctx = current_job()
    return ctx.usage if ctx is not None else None


def record(**kwargs) -> None:
    """Record one AI call into the active review's tracker (no-op outside one)."""
    tracker = current_tracker()
    if tracker is not None:
        tracker.record(**kwargs)


class UsageLog:
    """One entry per review/dialogue: the `usage` table in state/reviewer.db
    (what /stats aggregates, in SQL) plus logs/usage.jsonl, still written in
    parallel for a while (grep-able, and the rollback path). Fail-open — the
    logs dir can be unwritable (bind-mount ownership); accounting never
    breaks a review."""

    def __init__(self, log_dir: str | Path, db: Database | None = None) -> None:
        self.path = Path(log_dir) / "usage.jsonl"
        self.db = db if db is not None else Database.memory()

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
        self.insert(entry)
        try:
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("usage.jsonl append failed (%s)", exc)

    def insert(self, entry: dict) -> None:
        """One entry into the usage table (also the usage.jsonl import path)."""
        try:
            self.db.execute(
                "INSERT INTO usage (ts, kind, instance, project, mr_iid, input_tokens, "
                "cached_tokens, cache_savings_usd, output_tokens, cost_usd, entry) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.get("ts") or "", entry.get("kind") or "review",
                 entry.get("instance") or "", entry.get("project") or "",
                 entry.get("mr_iid"), entry.get("input_tokens", 0),
                 entry.get("cached_tokens", 0), entry.get("cache_savings_usd", 0.0),
                 entry.get("output_tokens", 0), entry.get("cost_usd", 0.0),
                 json.dumps(entry, ensure_ascii=False)))
        except sqlite3.Error as exc:
            logger.warning("usage persist failed (%s) — stats entry lost", exc)

    def aggregate(self) -> dict:
        """Overall stats for the /stats endpoint and dashboard (SQL over the
        usage table — no longer a full read of usage.jsonl per request)."""
        q = self.db.query
        row = q("SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), "
                "COALESCE(SUM(cached_tokens), 0), COALESCE(SUM(output_tokens), 0), "
                "COALESCE(SUM(cost_usd), 0), COALESCE(SUM(cache_savings_usd), 0) "
                "FROM usage")[0]
        totals = {"reviews": row[0], "input_tokens": row[1], "cached_tokens": row[2],
                  "output_tokens": row[3], "cost_usd": round(row[4], 6),
                  "cache_savings_usd": round(row[5], 6)}
        models: dict[str, dict] = {}
        for m in q("SELECT m.key, "
                   "SUM(COALESCE(json_extract(m.value, '$.calls'), 0)), "
                   "SUM(COALESCE(json_extract(m.value, '$.input_tokens'), 0)), "
                   "SUM(COALESCE(json_extract(m.value, '$.cached_tokens'), 0)), "
                   "SUM(COALESCE(json_extract(m.value, '$.cache_read_tokens'), 0)), "
                   "SUM(COALESCE(json_extract(m.value, '$.cache_creation_tokens'), 0)), "
                   "SUM(COALESCE(json_extract(m.value, '$.output_tokens'), 0)), "
                   "SUM(COALESCE(json_extract(m.value, '$.cost_usd'), 0)) "
                   "FROM usage, json_each(usage.entry, '$.models') AS m "
                   "GROUP BY m.key ORDER BY m.key"):
            models[m[0]] = {"calls": m[1], "input_tokens": m[2], "cached_tokens": m[3],
                            "cache_read_tokens": m[4], "cache_creation_tokens": m[5],
                            "output_tokens": m[6], "cost_usd": round(m[7], 6)}
        daily = {d[0]: {"reviews": d[1], "cost_usd": round(d[2], 6)}
                 for d in q("SELECT substr(ts, 1, 10) AS day, COUNT(*), SUM(cost_usd) "
                            "FROM usage WHERE ts != '' GROUP BY day ORDER BY day")}
        recent = [json.loads(r[0]) for r in
                  q("SELECT entry FROM usage ORDER BY id DESC LIMIT 20")][::-1]
        return {"totals": totals, "by_model": models, "daily": daily, "recent": recent}
