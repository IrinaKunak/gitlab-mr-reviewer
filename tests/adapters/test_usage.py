"""Usage accounting and pricing (curated table, MODEL_PRICES, OpenRouter catalog)."""

from __future__ import annotations

from reviewer.config import Settings
from reviewer.logging_setup import job_context
from tests.factories import (
    review_job,
)


def test_usage_cost_and_tracker(tmp_path, monkeypatch):
    from reviewer import usage

    # dated model ids normalize to the priced alias
    pricing = usage.Pricing()
    assert pricing.model_key("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert pricing.cost_usd("claude-sonnet-5", 100_000, 10_000) == (
        100_000 * 2.0 + 10_000 * 10.0) / 1_000_000  # $0.30
    assert pricing.cost_usd("unknown/model", 1_000_000, 0) == 0.0  # unknown -> $0

    tracker = usage.UsageTracker()
    tracker.record(tier="fast", model="claude-haiku-4-5-20251001",
                   provider="gateway", input_tokens=19_000, output_tokens=450)
    tracker.record(tier="main", model="claude-sonnet-5", provider="gateway",
                   input_tokens=100_000, output_tokens=7_500)
    tracker.record(tier="smart", model="claude-opus-4-8", provider="gateway",
                   input_tokens=300_000, output_tokens=7_000)
    assert tracker.total_input == 419_000
    assert set(tracker.by_model()) == {
        "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"}
    assert 1.9 < tracker.total_cost < 2.1  # ≈ $0.02 + $0.28 + $1.68
    assert "$" in tracker.summary_line()

    # persist + aggregate roundtrip
    log = usage.UsageLog(tmp_path)
    job = review_job(project_path="g/p", mr_iid=7)
    log.persist(tracker, job)
    log.persist(tracker, job)
    agg = log.aggregate()
    assert agg["totals"]["reviews"] == 2
    assert agg["totals"]["input_tokens"] == 2 * 419_000
    assert agg["by_model"]["claude-opus-4-8"]["calls"] == 2
    assert len(agg["recent"]) == 2
    # daily rollup for the dashboard: both entries land on today's UTC date
    assert len(agg["daily"]) == 1
    (day_stats,) = agg["daily"].values()
    assert day_stats["reviews"] == 2

    # the dashboard page is self-contained and wired to /stats
    from reviewer.dashboard import DASHBOARD_HTML
    assert '"/stats"' in DASHBOARD_HTML
    assert '<svg id="daily"' in DASHBOARD_HTML
    # cache visibility: tile + per-model tooltip + recent-reviews column, all
    # fed by fields aggregate() actually emits
    assert "Prompt cache" in DASHBOARD_HTML
    assert "cache_savings_usd" in DASHBOARD_HTML
    assert "% of input from cache" in DASHBOARD_HTML
    assert '<th class="num">cached</th>' in DASHBOARD_HTML
    for field in ("cached_tokens", "cache_savings_usd"):
        assert field in agg["totals"], field
    assert "cached_tokens" in agg["by_model"]["claude-opus-4-8"]
    # fully self-contained: no external asset/script URLs anywhere
    assert "https://" not in DASHBOARD_HTML and "http://" not in DASHBOARD_HTML

    # telegram footer, AIManager style
    from reviewer.adapters.notify.telegram import usage_footer

    footer = usage_footer(tracker.summary())
    assert "haiku-4-5: →19000 ←450" in footer
    assert "💰$" in footer

    # contextvar plumbing: record() is a no-op without an active tracker
    usage.record(tier="fast", model="m", provider="p",
                 input_tokens=1, output_tokens=1)
    with job_context(usage=usage.UsageTracker()):
        usage.record(tier="fast", model="claude-haiku-4-5", provider="gateway",
                     input_tokens=5, output_tokens=5)
        assert usage.current_tracker().calls[0]["input_tokens"] == 5
    assert usage.current_tracker() is None


def test_openrouter_catalog_prices_unknown_models(tmp_path, monkeypatch):
    # any OpenRouter model can be a tier override -> its cost must be priced
    # from the live catalog, not silently $0
    from reviewer import openrouter_models, usage

    raw = {"data": [
        {"id": "z-ai/glm-5", "pricing": {"prompt": "0.0000006", "completion": "0.0000022"}},
        {"id": "qwen/qwen4-coder", "pricing": {"prompt": "0.0000003", "completion": "0.0000012"}},
        {"id": "broken/model", "pricing": {"prompt": "n/a"}},  # unparseable -> skipped
        {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
    ]}
    parsed = openrouter_models._parse(raw)
    assert parsed["z-ai/glm-5"] == (0.6, 2.2)          # $/token -> $/MTok
    assert "broken/model" not in parsed
    assert "openrouter/auto" not in parsed             # negative sentinel filtered

    # feed the catalog in and confirm cost_usd uses it for an unknown model
    def _boom(proxy_url):
        raise RuntimeError("no network")
    catalog = openrouter_models.OpenRouterCatalog(tmp_path, fetch=_boom)
    catalog.store(parsed, 1e18)  # never stale
    pricing = usage.Pricing(catalog=catalog)
    assert pricing.price_of("z-ai/glm-5") == (0.6, 2.2)
    cost = pricing.cost_usd("z-ai/glm-5", 1_000_000, 1_000_000)
    assert abs(cost - (0.6 + 2.2)) < 1e-9
    # curated prices still win over the catalog
    assert pricing.price_of("claude-opus-4-8") == (5.0, 25.0)
    # opus-5 (released 2026-07-24) is priced like 4.8 on both routes — an
    # unpriced tier model would silently cost $0 in the stats
    assert pricing.price_of("claude-opus-5") == (5.0, 25.0)
    assert pricing.price_of("anthropic/claude-opus-5") == (5.0, 25.0)
    assert pricing.price_of(Settings().llm.tiers.smart.model) != (0.0, 0.0)
    # a genuinely unknown model is $0 (fail-open), not a crash
    assert pricing.price_of("totally/unknown") == (0.0, 0.0)

    # refresh() is fail-open: a network error keeps the cached copy
    catalog.store(parsed, 0.0)  # stale -> forces a refresh attempt
    assert catalog.refresh() == parsed  # falls back to cache, no raise
