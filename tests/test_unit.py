"""Offline unit tests for v2 (no network, no API keys needed).

Run: .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import time

import pytest

from reviewer import ai_client as ai_mod
from reviewer import bridge as bridge_mod
from reviewer import gitlab_io
from reviewer.ai_client import AIClient, extract_json
from reviewer.config import Settings
from reviewer.repo_cache import _safe_path, repo_grep, repo_list_tree, repo_read_file
from reviewer.server import ReviewQueue


# --- config ---

def test_proxy_precedence(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://p:1")
    monkeypatch.setenv("SOCKS_PROXY", "s:2")
    cfg = Settings()
    assert cfg.proxy_url == "http://p:1"
    assert cfg.requests_proxies == {"http": "http://p:1", "https": "http://p:1"}
    monkeypatch.delenv("HTTP_PROXY")
    cfg = Settings()
    assert cfg.proxy_url == "socks5://s:2"
    assert cfg.requests_proxies["https"] == "socks5h://s:2"


def test_instances_routing_keyed_by_webhook_token(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("GITLAB_", "XGITLABTOKEN")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITLAB_URL", "https://a")
    monkeypatch.setenv("GITLAB_TOKEN", "t1")
    monkeypatch.setenv("XGITLABTOKEN", "hook1")
    monkeypatch.setenv("GITLAB_URL_2", "https://b")
    monkeypatch.setenv("GITLAB_TOKEN_2", "t2")
    monkeypatch.setenv("XGITLABTOKEN_2", "hook2")
    cfg = Settings()
    assert cfg.gitlab_instances["hook1"]["name"] == "primary"
    assert cfg.gitlab_instances["hook2"]["url"] == "https://b"


def test_tier_mapping_and_fallback_chains():
    cfg = Settings()
    assert cfg.model_for_tier("fast") == cfg.model_fast
    assert cfg.fallback_chain("smart")[0].startswith("anthropic/")


# --- ai_client helpers ---

def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('prose\n```json\n{"a": 1}\n```\nmore') == {"a": 1}
    assert extract_json('text {"a": {"b": 2}} tail') == {"a": {"b": 2}}
    assert extract_json("no json here") is None


def test_cache_roundtrip(tmp_path):
    cfg = Settings()
    cfg.ai_cache_dir = str(tmp_path)
    cfg.ai_cache_ttl = 3600
    client = AIClient(cfg)
    key = client._cache_key("m", "sys", "user")
    assert client._cache_get(key) is None
    client._cache_put(key, "result text")
    assert client._cache_get(key) == "result text"
    # expired entries are misses
    old = tmp_path / key
    import os
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert client._cache_get(key) is None


def test_debug_log_failure_is_nonfatal(tmp_path):
    # regression: unwritable logs/ mount raised Errno 13 inside _debug and killed the review
    cfg = Settings()
    cfg.ai_debug = True
    blocker = tmp_path / "blocker"
    blocker.write_text("")  # file where a directory is needed -> mkdir raises OSError
    cfg.ai_log_dir = str(blocker / "logs")
    client = AIClient(cfg)
    client._debug("request", "payload")  # must not raise
    assert client._debug_failed
    client._debug("response", "again")   # stays disabled, still no raise


def test_truncated_empty_response_retries_with_larger_budget(tmp_path):
    # regression: sonnet-5 adaptive thinking ate the whole 4096 budget on a huge
    # diff -> zero text blocks -> a junk marker-only comment was posted AND cached
    import asyncio
    from types import SimpleNamespace
    from reviewer.ai_client import TRUNCATION_MARKER

    cfg = Settings()
    cfg.ai_cache_dir = str(tmp_path)
    client = AIClient(cfg)
    budgets = []

    def fake_response(texts, stop):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=t) for t in texts],
            stop_reason=stop, model="m",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    async def fake_create(**kwargs):
        budgets.append(kwargs["max_tokens"])
        if len(budgets) == 1:
            return fake_response([], "max_tokens")  # thinking consumed everything
        return fake_response(["real review"], "end_turn")

    client._primary = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    result = asyncio.run(client.complete("main", "sys", "user", max_tokens=4096))
    assert budgets == [4096, 16384]
    assert result.text == "real review"

    # if the retry ALSO comes back with no text, the marker-only result must not
    # be cached (a poisoned cache entry made every retrigger junk for an hour)
    async def always_empty(**kwargs):
        return fake_response([], "max_tokens")

    client._primary = SimpleNamespace(messages=SimpleNamespace(create=always_empty))
    result2 = asyncio.run(client.complete("main", "sys2", "user2", max_tokens=4096))
    assert result2.text == TRUNCATION_MARKER
    key = client._cache_key(cfg.model_for_tier("main"), "sys2", "user2")
    assert client._cache_get(key) is None


def test_gateway_auth_modes():
    # mode 1: real key + cfut token -> both x-api-key and cf-aig-authorization
    cfg = Settings()
    cfg.anthropic_api_url = "https://gateway.example/anthropic"
    cfg.anthropic_api_key = "sk-ant-real"
    cfg.anthropic_gateway_key = "cfut_abc123"
    primary = AIClient(cfg).primary
    assert primary.api_key == "sk-ant-real"
    assert primary.default_headers.get("cf-aig-authorization") == "Bearer cfut_abc123"

    # mode 2: cfut only (Unified Billing) -> header + dummy x-api-key
    cfg2 = Settings()
    cfg2.anthropic_api_url = "https://gateway.example/anthropic"
    cfg2.anthropic_api_key = ""
    cfg2.anthropic_gateway_key = "cfut_abc123"
    primary2 = AIClient(cfg2).primary
    assert primary2.api_key == "gateway"
    assert primary2.default_headers.get("cf-aig-authorization") == "Bearer cfut_abc123"

    # mode 3 / legacy: real key stored in the GATEWAY var -> plain x-api-key
    cfg3 = Settings()
    cfg3.anthropic_api_url = "https://gateway.example/anthropic"
    cfg3.anthropic_api_key = ""
    cfg3.anthropic_gateway_key = "sk-ant-real"
    primary3 = AIClient(cfg3).primary
    assert primary3.api_key == "sk-ant-real"
    assert primary3.default_headers.get("cf-aig-authorization") is None


def test_primary_params_per_tier():
    client = AIClient(Settings())
    assert client._primary_params("fast", None) == {}        # Haiku: no thinking/effort
    smart = client._primary_params("smart", "high")
    assert smart["thinking"] == {"type": "adaptive"}
    assert smart["output_config"] == {"effort": "high"}
    # main tier: thinking EXPLICITLY disabled — sonnet-5 runs adaptive thinking
    # when the param is omitted (changed from sonnet-4-6), and it consumed the
    # whole max_tokens budget before any text on big diffs (prod 2026-07-22)
    assert client._primary_params("main", None) == {"thinking": {"type": "disabled"}}
    assert client._primary_params("main", "high") == {"thinking": {"type": "disabled"}}


def test_input_size_guard():
    cfg = Settings()
    cfg.ai_max_input_tokens = 10
    client = AIClient(cfg)
    with pytest.raises(ai_mod.AIInputTooLargeError):
        client.guard_input_size("x" * 1000)


# --- bridge ---

def test_strip_usage_footer():
    text = "Issue PBV-123 is about cart totals.\nAcceptance: totals match.\nsonnet: 12.3k tok $0.04"
    assert bridge_mod.strip_usage_footer(text).endswith("totals match.")
    # multi-line answers without a footer are untouched
    clean = "line one\nline two"
    assert bridge_mod.strip_usage_footer(clean) == clean
    # a colon line WITHOUT digits is content, not a footer
    keep = "Steps:\nDo the thing:"
    assert bridge_mod.strip_usage_footer(keep) == keep


def test_rate_window():
    window = bridge_mod._RateWindow(2)
    assert window.allow() and window.allow()
    assert not window.allow()


# --- repo tools sandbox ---

def test_repo_tools(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def handle_cart():\n    return 42\n")
    (tmp_path / "README.md").write_text("hello")

    hits = repo_grep(tmp_path, r"handle_cart")
    assert "src/app.py:1" in hits

    content = repo_read_file(tmp_path, "src/app.py")
    assert "1: def handle_cart():" in content

    tree = repo_list_tree(tmp_path)
    assert "src/" in tree and "README.md" in tree

    with pytest.raises(ValueError):
        _safe_path(tmp_path, "../../etc/passwd")
    assert "Error" in repo_read_file(tmp_path, "../secret")


# --- gitlab_io ---

WEBHOOK_PAYLOAD = {
    "object_attributes": {
        "action": "open", "iid": 7, "id": 100,
        "source_branch": "PBV-123-fix-cart", "target_branch": "master",
        "title": "Fix cart", "description": "closes ABC-9",
        "url": "https://lab/x/-/mergerequests/7",
        "last_commit": {"id": "deadbeef"},
    },
    "project": {"id": 1, "path_with_namespace": "g/x", "name": "x"},
    "user": {"username": "max", "name": "Max"},
}


def test_usage_cost_and_tracker(tmp_path, monkeypatch):
    from reviewer import usage

    # dated model ids normalize to the priced alias
    assert usage.model_key("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert usage.cost_usd("claude-sonnet-5", 100_000, 10_000) == (
        100_000 * 2.0 + 10_000 * 10.0) / 1_000_000  # $0.30
    assert usage.cost_usd("unknown/model", 1_000_000, 0) == 0.0  # unknown -> $0

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
    monkeypatch.setattr(usage.settings, "ai_log_dir", str(tmp_path))
    mr = {"gitlab_config": {"name": "primary"}, "project_path": "g/p", "mr_iid": 7}
    usage.persist(tracker, mr)
    usage.persist(tracker, mr)
    agg = usage.aggregate()
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
    footer = tracker.footer_line()
    assert "haiku-4-5: →19000 ←450" in footer
    assert "💰$" in footer

    # contextvar plumbing: record() is a no-op without an active tracker
    usage.record(tier="fast", model="m", provider="p",
                 input_tokens=1, output_tokens=1)
    token = usage.current_tracker.set(usage.UsageTracker())
    usage.record(tier="fast", model="claude-haiku-4-5", provider="gateway",
                 input_tokens=5, output_tokens=5)
    assert usage.current_tracker.get().calls[0]["input_tokens"] == 5
    usage.current_tracker.reset(token)


def test_openrouter_catalog_prices_unknown_models(tmp_path, monkeypatch):
    # any OpenRouter model can be a tier override -> its cost must be priced
    # from the live catalog, not silently $0
    from reviewer import openrouter_models, usage
    from reviewer.config import settings

    monkeypatch.setattr(settings, "ai_cache_dir", str(tmp_path))
    monkeypatch.setattr(openrouter_models, "_cache", None)
    monkeypatch.setattr(openrouter_models, "_fetched_at", 0.0)

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
    monkeypatch.setattr(openrouter_models, "_cache", parsed)
    monkeypatch.setattr(openrouter_models, "_fetched_at", 1e18)  # never stale
    assert usage.price_of("z-ai/glm-5") == (0.6, 2.2)
    cost = usage.cost_usd("z-ai/glm-5", 1_000_000, 1_000_000)
    assert abs(cost - (0.6 + 2.2)) < 1e-9
    # curated prices still win over the catalog
    assert usage.price_of("claude-opus-4-8") == (5.0, 25.0)
    # a genuinely unknown model is $0 (fail-open), not a crash
    assert usage.price_of("totally/unknown") == (0.0, 0.0)

    # refresh() is fail-open: a network error keeps the cached copy
    def _boom():
        raise RuntimeError("no network")
    monkeypatch.setattr(openrouter_models, "_fetch_now", _boom)
    monkeypatch.setattr(openrouter_models, "_fetched_at", 0.0)  # force a refresh attempt
    assert openrouter_models.refresh() == parsed  # falls back to cache, no raise


def test_agent_loop_marks_prompt_cache_breakpoints(tmp_path, monkeypatch):
    # Anthropic caching is opt-in: without cache_control every investigator
    # iteration re-bills the whole repo/diff prefix (that is why the same loop
    # cost ~2x more on the CF gateway than on auto-caching OpenRouter models)
    import asyncio
    from types import SimpleNamespace
    from reviewer.ai_client import ToolDef

    cfg = Settings()
    cfg.ai_cache_dir = str(tmp_path)
    cfg.ai_rate_limit = 0
    monkeypatch.setattr(ai_mod.overrides, "model_for_tier",
                        lambda tier, c: c.model_for_tier(tier))
    client = AIClient(cfg)
    sent: list[list[dict]] = []
    turns = iter(["tool_use", "tool_use", "end_turn"])

    def tool_use_response():
        return SimpleNamespace(
            stop_reason="tool_use", model="claude-opus-4-8",
            content=[SimpleNamespace(type="tool_use", id="t1", name="t", input={})],
            usage=SimpleNamespace(input_tokens=5, output_tokens=5))

    async def fake_create(**kwargs):
        sent.append(kwargs["messages"])
        if next(turns) == "tool_use":
            return tool_use_response()
        return SimpleNamespace(
            stop_reason="end_turn", model="claude-opus-4-8",
            content=[SimpleNamespace(type="text", text="done")],
            usage=SimpleNamespace(input_tokens=5, output_tokens=5))

    stub = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    stub.with_options = lambda **kw: stub
    client._primary = stub

    async def handler(**kw):
        return "tool output"

    asyncio.run(client.agent_loop(
        "smart", "sys", "big diff", [ToolDef("t", "d", {}, handler)],
        max_iterations=5))

    def breakpoints(messages):
        return [b for m in messages for b in m["content"]
                if isinstance(b, dict) and "cache_control" in b]

    # the static prefix (tools + system render ahead of it) is always cached
    assert sent[0][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # ...and a single rolling breakpoint follows the growing history — never
    # more than the API's 4-per-request limit, however long the loop runs
    for messages in sent:
        assert 1 <= len(breakpoints(messages)) <= 4
    assert len(breakpoints(sent[-1])) == 2      # static prefix + latest turn
    assert sent[-1][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    # OpenRouter models auto-cache and may reject the marker — strip it there
    stripped = ai_mod._strip_cache_control(sent[-1])
    assert breakpoints(stripped) == []
    assert stripped[0]["content"][0]["text"] == "big diff"  # content preserved


def test_cached_prompt_tokens_are_counted(tmp_path, monkeypatch):
    # regression: gpt-5.6-terra via OpenRouter reported →9 input tokens on a
    # 3-iteration investigation (prod 2026-07-23) — wire-format input_tokens
    # EXCLUDES cached tokens, and auto-caching models put nearly the whole
    # prompt in cache_read_input_tokens, which we silently dropped
    import asyncio
    from types import SimpleNamespace
    from reviewer import usage
    from reviewer.ai_client import ToolDef

    tracker = usage.UsageTracker()
    tracker.record(tier="smart", model="openai/gpt-5.6-terra",
                   provider="openrouter", input_tokens=9, output_tokens=4029,
                   cache_read_tokens=150_000, cache_creation_tokens=10_000)
    call = tracker.calls[0]
    assert call["input_tokens"] == 160_009        # full amount the model read
    assert call["cached_tokens"] == 160_000
    expected = (9 * 2.5 + 150_000 * 2.5 * 0.1 + 10_000 * 2.5 * 1.25
                + 4029 * 15.0) / 1_000_000        # reads 0.1x, writes 1.25x
    assert abs(call["cost_usd"] - expected) < 1e-9
    assert "→160009" in tracker.footer_line()

    # cache savings are NET: reads save 0.9x of the input rate, cache writes
    # cost a 0.25x premium — a write-only review must not look like a win
    expected_saved = (150_000 * 2.5 * 0.9 - 10_000 * 2.5 * 0.25) / 1_000_000
    assert abs(tracker.cache_savings() - expected_saved) < 1e-9
    write_only = usage.UsageTracker()
    write_only.record(tier="smart", model="claude-opus-4-8", provider="gateway",
                      input_tokens=100, output_tokens=10,
                      cache_creation_tokens=100_000)
    assert write_only.cache_savings() < 0

    # _to_result and agent_loop must both pick the cache fields off the wire
    cfg = Settings()
    cfg.ai_cache_dir = str(tmp_path)
    cfg.ai_rate_limit = 0
    # a dev-machine cache/model_overrides.json must not reroute this test
    monkeypatch.setattr(ai_mod.overrides, "model_for_tier",
                        lambda tier, c: c.model_for_tier(tier))
    client = AIClient(cfg)
    resp = SimpleNamespace(
        stop_reason="end_turn", model="openai/gpt-5.6-terra",
        content=[SimpleNamespace(type="text", text="done")],
        usage=SimpleNamespace(input_tokens=9, output_tokens=100,
                              cache_read_input_tokens=50_000,
                              cache_creation_input_tokens=2_000))
    result = client._to_result(resp, "openrouter")
    assert result.cache_read_tokens == 50_000
    assert result.cache_creation_tokens == 2_000

    async def fake_create(**kwargs):
        return resp

    stub = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    stub.with_options = lambda **kw: stub
    client._primary = stub
    token = usage.current_tracker.set(usage.UsageTracker())
    try:
        loop_result = asyncio.run(client.agent_loop(
            "smart", "sys", "user", [ToolDef("t", "d", {}, None)],
            max_iterations=2))
        assert loop_result.cache_read_tokens == 50_000
        recorded = usage.current_tracker.get().calls[-1]
        assert recorded["input_tokens"] == 9 + 50_000 + 2_000
        assert recorded["cached_tokens"] == 52_000
    finally:
        usage.current_tracker.reset(token)


def test_model_overrides_and_routing(tmp_path, monkeypatch):
    # owner feature 2026-07-23: switch tier models at runtime from the dashboard;
    # vendor-prefixed overrides must route via OpenRouter, claude-* via gateway
    import asyncio
    from types import SimpleNamespace
    from reviewer import overrides
    from reviewer.config import settings as live_settings

    monkeypatch.setattr(live_settings, "ai_cache_dir", str(tmp_path))
    monkeypatch.setattr(overrides, "_cache", None)

    cfg = Settings()
    cfg.ai_cache_dir = str(tmp_path)
    assert overrides.model_for_tier("smart", cfg) == cfg.model_smart  # env default
    overrides.save({"smart": "openai/gpt-5.6-terra", "junk": "ignored"})
    assert overrides.model_for_tier("smart", cfg) == "openai/gpt-5.6-terra"
    assert overrides.load()["fast"] == ""  # untouched tiers stay on defaults

    # complete() with the override must call the OpenRouter client, not primary
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn", model="openai/gpt-5.6-terra",
            usage=SimpleNamespace(input_tokens=5, output_tokens=5))

    client = AIClient(cfg)
    client._fallback = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    client._primary = None  # would explode if touched — proves routing
    result = asyncio.run(client.complete("smart", "sys", "user", use_cache=False))
    assert result.provider == "openrouter"
    assert captured["model"] == "openai/gpt-5.6-terra"
    assert captured["extra_body"]["models"][0] == "openai/gpt-5.6-terra"

    monkeypatch.setattr(overrides, "_cache", None)  # don't leak into other tests


def test_basic_auth(monkeypatch):
    import base64
    from reviewer.config import settings
    from reviewer.server import basic_auth_ok, stats_access_allowed

    monkeypatch.setattr(settings, "stats_user", "max")
    monkeypatch.setattr(settings, "stats_password", "pw123")
    good = "Basic " + base64.b64encode(b"max:pw123").decode()
    bad = "Basic " + base64.b64encode(b"max:nope").decode()
    assert basic_auth_ok(good) is True
    assert basic_auth_ok(bad) is False
    assert basic_auth_ok("Bearer xyz") is False
    # with basic configured, unauthenticated local access is no longer allowed
    monkeypatch.setattr(settings, "stats_token", "")
    assert stats_access_allowed(good, "", "203.0.113.7") is True
    assert stats_access_allowed("", "", None) is False


def test_stats_access_control(monkeypatch):
    from reviewer.config import settings
    from reviewer.server import stats_access_allowed

    # no token configured: only direct (non-proxied) requests pass
    monkeypatch.setattr(settings, "stats_token", "")
    assert stats_access_allowed("", "", None) is True
    assert stats_access_allowed("", "", "203.0.113.7") is False

    # token configured: Bearer header or ?token= must match exactly
    monkeypatch.setattr(settings, "stats_token", "s3cret")
    assert stats_access_allowed("Bearer s3cret", "", "203.0.113.7") is True
    assert stats_access_allowed("", "s3cret", "203.0.113.7") is True
    assert stats_access_allowed("Bearer wrong", "", None) is False
    assert stats_access_allowed("", "", None) is False  # token set: local needs it too


def test_agent_loop_records_usage_on_max_iterations(tmp_path):
    # regression: only the clean end_turn exit recorded usage — investigations
    # that hit max_iterations or died mid-loop vanished from the stats
    import asyncio
    from types import SimpleNamespace
    from reviewer import usage
    from reviewer.ai_client import AIClient

    cfg = Settings()
    cfg.ai_cache_dir = str(tmp_path)
    client = AIClient(cfg)

    tool_block = SimpleNamespace(type="tool_use", name="nope", input={}, id="t1")

    async def fake_create(**kwargs):
        return SimpleNamespace(
            content=[tool_block], stop_reason="tool_use", model="m",
            usage=SimpleNamespace(input_tokens=100, output_tokens=10))

    stub = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    stub.with_options = lambda **kw: stub
    client._primary = stub
    tracker = usage.UsageTracker()
    token = usage.current_tracker.set(tracker)
    try:
        result = asyncio.run(client.agent_loop(
            "smart", "sys", "go", tools=[], max_iterations=3))
    finally:
        usage.current_tracker.reset(token)

    assert result.input_tokens == 300 and result.output_tokens == 30
    assert len(tracker.calls) == 1  # a single aggregate record for the loop
    assert tracker.calls[0]["input_tokens"] == 300
    assert tracker.calls[0]["output_tokens"] == 30


def test_tester_report_targets(monkeypatch):
    # owner request 2026-07-23: reports go to the team group(s) too, not only
    # the bridge chat where AIManager archives them
    from reviewer.config import settings
    from reviewer.pipeline import tester_report_targets

    monkeypatch.setattr(settings, "bridge_chat_id", "-100bridge")
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_chat_ids", ["-100team", "-100extra"])
    monkeypatch.setattr(settings, "tester_report_chat_ids", [])
    assert tester_report_targets() == ["-100bridge", "-100team", "-100extra"]

    # explicit override narrows the team targets; dedupe against bridge
    monkeypatch.setattr(settings, "tester_report_chat_ids", ["-100team", "-100bridge"])
    assert tester_report_targets() == ["-100bridge", "-100team"]

    # telegram off -> only the bridge copy
    monkeypatch.setattr(settings, "telegram_enabled", False)
    assert tester_report_targets() == ["-100bridge"]


def test_split_investigation():
    # regression: with TESTER_REPORT=off the whole investigation (impact analysis
    # included) was silently discarded — only the tester report is flag-gated
    from reviewer.pipeline import split_investigation

    impact, report = split_investigation(
        "Impact: touches auth.\n\n## TESTER REPORT\n\nVerify login.")
    assert impact == "Impact: touches auth."
    assert report == "## TESTER REPORT\n\nVerify login."

    impact2, report2 = split_investigation("Analysis only, no report section.")
    assert impact2 == "Analysis only, no report section."
    assert report2 is None


def test_translate_guard_rejects_non_cyrillic_output(monkeypatch):
    # regression: Haiku answered the translate request with English commentary
    # ("you haven't provided a markdown document") and it was posted as the review
    import asyncio
    from types import SimpleNamespace
    from reviewer.config import settings
    from reviewer.pipeline import Pipeline

    monkeypatch.setattr(settings, "review_language", "ru")
    answers = iter([
        "I appreciate your message, but you haven't provided a document.",
        "Обзор: всё в порядке.",
    ])

    class StubAI:
        async def complete(self, tier, system, user, **kwargs):
            assert "<document>" in user  # translation input is always wrapped now
            return SimpleNamespace(text=next(answers))

    p = Pipeline(client=StubAI())
    # commentary (no Cyrillic) -> deliver the English original instead
    assert asyncio.run(p._translate_if_needed("review text", "fast")) == "review text"
    # real translation passes through
    assert asyncio.run(p._translate_if_needed("review text", "fast")) == "Обзор: всё в порядке."


def test_process_skips_merged_or_closed_mr(monkeypatch):
    # an update webhook can sit in the queue while the MR gets merged/closed
    # (push fix -> merge on green); the worker must not review it then
    import asyncio
    from types import SimpleNamespace
    from reviewer import pipeline as pipeline_mod

    for state in ("merged", "closed"):
        stub_mr = SimpleNamespace(state=state)
        stub_project = SimpleNamespace(
            mergerequests=SimpleNamespace(get=lambda iid: stub_mr),
            path_with_namespace="group/proj")
        stub_gl = SimpleNamespace(projects=SimpleNamespace(get=lambda pid: stub_project))
        monkeypatch.setattr(pipeline_mod.gitlab_io, "get_gitlab_client",
                            lambda cfg: stub_gl)

        def _boom(*args, **kwargs):
            raise AssertionError(f"must not run for a {stub_mr.state} MR")

        monkeypatch.setattr(pipeline_mod.gitlab_io, "check_merge_conflicts", _boom)
        monkeypatch.setattr(pipeline_mod.telegram_io, "notify", _boom)

        p = pipeline_mod.Pipeline(client=object())  # AI must never be touched
        mr_data = {"project_id": 1, "mr_iid": 2, "title": "t"}
        asyncio.run(p._process_inner(
            mr_data, {"name": "primary", "url": "https://x"}, {}))


def test_review_state_roundtrip_and_bound(tmp_path, monkeypatch):
    from reviewer import review_state
    from reviewer.config import settings

    monkeypatch.setattr(settings, "ai_cache_dir", str(tmp_path))
    monkeypatch.setattr(review_state, "_cache", None)  # drop module-level cache

    assert review_state.get_last_sha("primary", 1, 2) is None
    review_state.set_last_sha("primary", 1, 2, "abc123")
    assert review_state.get_last_sha("primary", 1, 2) == "abc123"
    review_state.set_last_sha("primary", 1, 2, "def456")  # newer push wins
    assert review_state.get_last_sha("primary", 1, 2) == "def456"

    # survives a cold start (persisted to the cache volume)
    monkeypatch.setattr(review_state, "_cache", None)
    assert review_state.get_last_sha("primary", 1, 2) == "def456"

    # bounded: oldest entries evicted beyond MAX_ENTRIES
    monkeypatch.setattr(review_state, "MAX_ENTRIES", 3)
    for i in range(5):
        review_state.set_last_sha("primary", 100 + i, 1, f"sha{i}")
    assert review_state.get_last_sha("primary", 100, 1) is None
    assert review_state.get_last_sha("primary", 104, 1) == "sha4"


def test_incremental_review_helpers():
    # prompt contract for the anti-pedantry overhaul (dev feedback 2026-07-23)
    from types import SimpleNamespace
    from reviewer import prompts

    assert "## Verdict" in prompts.REVIEW_SYSTEM
    assert "INTENTIONAL" in prompts.REVIEW_SYSTEM      # no "confirm your decision"
    assert "No hypothetical concerns" in prompts.REVIEW_SYSTEM
    note = prompts.INCREMENTAL_REVIEW_NOTE.format(prev_sha="abc12345")
    assert "abc12345" in note and "delta" in note
    assert ".ai-review.md" in prompts.guidelines_section("Focus on SQL")

    # delta fetch wraps compare diffs into a changes-shaped dict; degrades to None
    stub = SimpleNamespace(
        repository_compare=lambda a, b: {"diffs": [{"new_path": "x.py", "diff": "+1"}]})
    delta = gitlab_io.fetch_delta_changes(stub, "aaa", "bbb")
    assert delta == {"changes": [{"new_path": "x.py", "diff": "+1"}]}
    stub_empty = SimpleNamespace(repository_compare=lambda a, b: {"diffs": []})
    assert gitlab_io.fetch_delta_changes(stub_empty, "aaa", "bbb") is None

    def _raise(a, b):
        raise RuntimeError("404 commit not found")
    stub_err = SimpleNamespace(repository_compare=_raise)
    assert gitlab_io.fetch_delta_changes(stub_err, "aaa", "bbb") is None

    # .ai-review.md is best-effort: absent file -> empty string
    class _Files:
        def get(self, path, ref):
            raise RuntimeError("404")
    assert gitlab_io.fetch_review_guidelines(
        SimpleNamespace(files=_Files()), "main") == ""


def test_triage_chooses_skipped_files_and_budget_truncation():
    # MR !779 (655 files, 235k tokens of diff) was refused outright as "MR too
    # large". 439 of those files were SVG/asset blobs — which files are worth
    # reading is a judgement call, so triage makes it; a hardcoded extension
    # list cannot know a project's conventions.
    from reviewer import prompts

    changes = {"changes": [
        {"new_path": "src/auth.py", "diff": "+def login():\n" * 50},
        {"new_path": "public/logo.svg", "diff": "+<path d='M0 0'/>\n" * 400},
        {"new_path": "yarn.lock", "diff": "+dep\n" * 300, "new_file": True},
        {"new_path": "src/pay.py", "diff": "+def charge():\n" * 50},
    ]}

    # the manifest triage judges from: status, size and path for every file
    manifest = gitlab_io.file_manifest(changes)
    assert "modified\t" in manifest and "public/logo.svg" in manifest
    assert "added\t" in manifest                     # yarn.lock is new_file
    assert manifest in prompts.triage_user_prompt({"title": "t"}, "diff", manifest)
    assert "skip_globs" in prompts.TRIAGE_SCHEMA["properties"]
    assert "skip_globs" in prompts.TRIAGE_SYSTEM

    # triage returns PATTERNS, not paths — listing 439 SVGs individually blew
    # the fast tier's max_tokens and the whole triage came back unparseable
    skip = gitlab_io.resolve_skip(changes, ["*.svg", "yarn.lock"])
    assert skip == {"public/logo.svg", "yarn.lock"}
    assert gitlab_io.resolve_skip(changes, ["public/*"]) == {"public/logo.svg"}
    # guards: a catch-all or an everything-matching verdict is discarded, so a
    # bad triage can never silence the review
    assert gitlab_io.resolve_skip(changes, ["*"]) == set()
    assert gitlab_io.resolve_skip(changes, ["*.py", "*.svg", "*.lock"]) == set()
    assert gitlab_io.resolve_skip(changes, "not-a-list") == set()

    # honouring triage's verdict keeps the code and names (not dumps) the rest
    out = gitlab_io.extract_diff_only(changes, skip=skip)
    assert "def login" in out and "def charge" in out
    assert "<path d=" not in out
    assert "SKIPPED — 2 changed file(s)" in out
    assert "deleted: " not in out and "modified: public/logo.svg" in out
    # no skip list -> unchanged v1 behaviour, everything included
    assert "<path d=" in gitlab_io.extract_diff_only(changes)

    # budget cap drops whole files and says so, instead of refusing the MR
    small = gitlab_io.extract_diff_only(changes, max_chars=800, skip=skip)
    assert len(small) < 2000
    assert "more changed file(s) omitted" in small
    assert "def login" in small                      # first file still reviewed


def test_investigator_degrades_before_cloning(monkeypatch):
    # prod !779: the investigator got the same full context the review had just
    # rejected as too large — and the guard only fires inside agent_loop, AFTER
    # the repo clone, so we paid for a clone then silently dropped the analysis
    from reviewer.config import settings
    from reviewer.pipeline import Pipeline

    monkeypatch.setattr(settings, "ai_max_input_tokens", 10_000)
    p = Pipeline(client=AIClient(Settings()))
    monkeypatch.setattr(p.ai.cfg, "ai_max_input_tokens", 10_000)
    triage, mr_data = {"jira_keys": [], "summary": "s", "risk_areas": []}, {"mr_iid": 779}

    huge, small = "x" * 200_000, "y" * 6_000
    # full context too big -> falls back to the diff, no exception, no clone yet
    assert p._investigator_content(mr_data, huge, triage, "review", small) == small
    # both too big -> a truncated subset, still something to investigate
    picked = p._investigator_content(mr_data, huge, triage, "review", huge)
    assert 0 < len(picked) < len(huge)
    # fits -> untouched
    assert p._investigator_content(mr_data, small, triage, "review", "z") == small


def test_force_full_re_review_marker():
    # a re-review label / [re-review] title marker forces a fresh full review
    # (regenerates the tester report on demand) and bypasses webhook dedupe,
    # since the label-add event carries the same sha the TTL window swallows
    from reviewer.server import ReviewQueue

    payload = {
        "object_attributes": {
            "action": "update", "iid": 5, "id": 50, "title": "INCR-54",
            "source_branch": "b", "target_branch": "master",
            "url": "https://x/-/merge_requests/5",
            "last_commit": {"id": "abc"}},
        "project": {"id": 170, "path_with_namespace": "g/p"},
        "user": {"username": "dev"},
        "labels": [{"title": "re-review"}],
    }
    parsed = gitlab_io.parse_merge_request_webhook(payload)
    assert parsed and parsed["force_full"] is True
    payload["labels"] = []
    assert gitlab_io.parse_merge_request_webhook(payload)["force_full"] is False
    payload["object_attributes"]["title"] = "INCR-54 [re-review]"
    assert gitlab_io.parse_merge_request_webhook(payload)["force_full"] is True

    q = ReviewQueue(workers=1, dedupe_ttl=600, burst_window=30)
    mr = {"gitlab_config": {"name": "primary"}, "project_id": 170,
          "mr_iid": 5, "last_commit": "abc"}
    assert q.submit(dict(mr)) is True
    assert q.submit(dict(mr)) is False                       # normal dedupe
    assert q.submit({**mr, "force_full": True}) is True      # forced through


def test_real_mr_author_and_comment_fetch():
    # webhook "user" is the EVENT ACTOR (title edit by the owner relabeled other
    # people's MRs as spikerwork) — the live MR object carries the real author
    from types import SimpleNamespace

    assert gitlab_io.real_mr_author(
        SimpleNamespace(author={"username": "nisvem"})) == "nisvem"
    assert gitlab_io.real_mr_author(SimpleNamespace(author=None)) == ""

    def note(author, body, system=False):
        return SimpleNamespace(author={"username": author}, body=body, system=system)

    notes = [
        note("gitlab", "added 1 commit", system=True),      # system -> skipped
        note("botuser", "## 🤖 Automated Code Review ..."),  # our own -> skipped
        note("irina", "это осознанное изменение, фабрика исключений"),
        note("artem", "каталог без бэка не бывает"),
    ]
    stub_mr = SimpleNamespace(notes=SimpleNamespace(list=lambda **kw: notes))
    text = gitlab_io.fetch_mr_comments(stub_mr, bot_username="botuser")
    assert "[irina]: это осознанное" in text
    assert "[artem]:" in text
    assert "Automated Code Review" not in text and "added 1 commit" not in text

    # oversized discussions keep the tail (latest replies), and API failures
    # must not break the review
    long_notes = [note("dev", f"comment {i} " + "x" * 500) for i in range(30)]
    stub_long = SimpleNamespace(notes=SimpleNamespace(list=lambda **kw: long_notes))
    capped = gitlab_io.fetch_mr_comments(stub_long, max_chars=2000)
    assert len(capped) <= 2001 and "comment 29" in capped

    def _raise(**kw):
        raise RuntimeError("403")
    broken = SimpleNamespace(notes=SimpleNamespace(list=_raise))
    assert gitlab_io.fetch_mr_comments(broken) == ""


def test_translate_long_text_upgrades_tier(monkeypatch):
    # dev feedback 2026-07-23: long reviews came back half-English from Haiku —
    # texts over the threshold must route to the main tier
    import asyncio
    from types import SimpleNamespace
    from reviewer.config import settings
    from reviewer.pipeline import Pipeline

    monkeypatch.setattr(settings, "review_language", "ru")
    tiers = []

    class StubAI:
        async def complete(self, tier, system, user, **kwargs):
            tiers.append(tier)
            return SimpleNamespace(text="Перевод готов.")

    p = Pipeline(client=StubAI())
    asyncio.run(p._translate_if_needed("short text", "fast"))
    asyncio.run(p._translate_if_needed("long text " * 500, "fast"))  # ~5000 chars
    assert tiers == ["fast", "main"]


def test_process_skips_already_reviewed_sha(monkeypatch):
    # metadata-only update webhooks (title/labels edits) re-arrive with the same
    # head sha we already reviewed — must skip before any notify/AI spend
    import asyncio
    from types import SimpleNamespace
    from reviewer import pipeline as pipeline_mod
    from reviewer import review_state

    stub_mr = SimpleNamespace(state="opened", sha="abc123")
    stub_project = SimpleNamespace(
        mergerequests=SimpleNamespace(get=lambda iid: stub_mr),
        path_with_namespace="group/proj")
    stub_gl = SimpleNamespace(projects=SimpleNamespace(get=lambda pid: stub_project))
    monkeypatch.setattr(pipeline_mod.gitlab_io, "get_gitlab_client", lambda cfg: stub_gl)
    monkeypatch.setattr(review_state, "get_last_sha", lambda *a: "abc123")

    def _boom(*args, **kwargs):
        raise AssertionError("must not run for an already-reviewed sha")

    monkeypatch.setattr(pipeline_mod.gitlab_io, "check_merge_conflicts", _boom)
    monkeypatch.setattr(pipeline_mod.telegram_io, "notify", _boom)

    p = pipeline_mod.Pipeline(client=object())
    mr_data = {"project_id": 1, "mr_iid": 2, "title": "t", "last_commit": "abc123"}
    asyncio.run(p._process_inner(mr_data, {"name": "primary", "url": "https://x"}, {}))


def test_burst_dedupe_collapses_multi_event_actions():
    # regression: reopening an MR after new pushes makes GitLab emit reopen +
    # update events with DIFFERENT shas ~1s apart -> two parallel reviews
    from reviewer.server import ReviewQueue

    def mr(sha):
        return {"gitlab_config": {"name": "primary"}, "project_id": 132,
                "mr_iid": 18, "last_commit": sha}

    q = ReviewQueue(workers=1, dedupe_ttl=600, burst_window=30)
    assert q.submit(mr("aaa")) is True
    assert q.submit(mr("bbb")) is False   # different sha, same MR, same instant
    assert q.submit(mr("aaa")) is False   # exact duplicate still deduped

    q2 = ReviewQueue(workers=1, dedupe_ttl=600, burst_window=0)
    assert q2.submit(mr("aaa")) is True
    assert q2.submit(mr("bbb")) is True   # window=0 disables burst collapsing


def test_review_content_handles_collapsed_diffs():
    # regression: GitLab returns empty diffs for collapsed (too large) files —
    # exactly the biggest files silently vanished from the review (MR !18)
    from types import SimpleNamespace
    from reviewer import gitlab_io

    class _File:
        def decode(self):
            return b"def core(): ...\n"

    project = SimpleNamespace(files=SimpleNamespace(get=lambda path, ref: _File()))
    mr = SimpleNamespace(source_branch="v2")
    changes = {"changes": [
        {"new_path": "reviewer/ai_client.py", "diff": "", "collapsed": True, "new_file": True},
        {"new_path": "small.py", "diff": "+ok", "new_file": True},
        {"new_path": "unchanged.py", "diff": ""},  # genuinely empty -> still skipped
    ]}
    out = gitlab_io.extract_review_content(project, mr, changes)
    assert "reviewer/ai_client.py" in out and "def core" in out
    assert "DIFF UNAVAILABLE" in out
    assert "unchanged.py" not in out

    diff_only = gitlab_io.extract_diff_only(changes)
    assert "[diff unavailable: file too large]" in diff_only


def test_parse_webhook_url_fix_and_actions():
    parsed = gitlab_io.parse_merge_request_webhook(WEBHOOK_PAYLOAD)
    assert parsed is not None
    assert parsed["url"] == "https://lab/x/-/merge_requests/7"  # contractual URL typo fix
    assert parsed["last_commit"] == "deadbeef"

    closed = {**WEBHOOK_PAYLOAD,
              "object_attributes": {**WEBHOOK_PAYLOAD["object_attributes"], "action": "close"}}
    assert gitlab_io.parse_merge_request_webhook(closed) is None


def test_parse_webhook_no_review_marker():
    tagged = {**WEBHOOK_PAYLOAD,
              "object_attributes": {**WEBHOOK_PAYLOAD["object_attributes"],
                                    "title": "big infra change [no-review]"}}
    assert gitlab_io.parse_merge_request_webhook(tagged) is None

    labeled = {**WEBHOOK_PAYLOAD, "labels": [{"title": "No-Review"}]}
    assert gitlab_io.parse_merge_request_webhook(labeled) is None


def test_extract_jira_keys():
    parsed = gitlab_io.parse_merge_request_webhook(WEBHOOK_PAYLOAD)
    keys = gitlab_io.extract_jira_keys(parsed)
    assert keys == ["PBV-123", "ABC-9"]


def test_format_review_comment_language(monkeypatch):
    from reviewer.config import settings
    monkeypatch.setattr(settings, "review_language", "ru")
    comment = gitlab_io.format_review_comment("текст обзора")
    assert "Автоматический обзор кода" in comment
    monkeypatch.setattr(settings, "review_language", "en")
    assert "Automated Code Review" in gitlab_io.format_review_comment("review")


# --- review-fix regressions ---

def test_should_fallback_gating():
    import httpx as _httpx

    def status_error(code):
        request = _httpx.Request("POST", "https://x")
        response = _httpx.Response(code, request=request)
        cls = {429: __import__("anthropic").RateLimitError}.get(code)
        if cls:
            return cls("e", response=response, body=None)
        import anthropic as _a
        return _a.APIStatusError("e", response=response, body=None)

    assert ai_mod._should_fallback(status_error(429)) is True
    assert ai_mod._should_fallback(status_error(500)) is True
    assert ai_mod._should_fallback(status_error(529)) is True
    assert ai_mod._should_fallback(status_error(400)) is False  # our bug — surface it
    assert ai_mod._should_fallback(status_error(401)) is False
    assert ai_mod._should_fallback(ValueError("x")) is False


def test_strip_thinking_blocks():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "a"},
            {"type": "tool_use", "id": "1", "name": "t", "input": {}},
        ]},
    ]
    cleaned = ai_mod._strip_thinking(messages)
    types = [b["type"] for b in cleaned[1]["content"]]
    assert types == ["text", "tool_use"]
    assert messages[1]["content"][0]["type"] == "thinking"  # original untouched


def test_safe_path_rejects_sibling_prefix(tmp_path):
    # /x/repo must not authorize /x/repo-evil (startswith-prefix traversal)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    sibling = tmp_path / "repo-evil"
    sibling.mkdir()
    (sibling / "secret").write_text("s")
    with pytest.raises(ValueError):
        _safe_path(worktree, "../repo-evil/secret")


def test_repo_tools_skip_symlinks(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("LEAKED_TOKEN=abc")
    (worktree / "link.txt").symlink_to(secret)
    (worktree / "ok.py").write_text("LEAKED_TOKEN nope, just code")
    hits = repo_grep(worktree, "LEAKED_TOKEN")
    assert "link.txt" not in hits and "ok.py" in hits
    # reading through the symlink must fail (resolve+sandbox check or symlink check)
    assert "LEAKED" not in repo_read_file(worktree, "link.txt")
    assert "link.txt" not in repo_list_tree(worktree)


def test_repo_read_file_edges(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\n")
    assert "beyond end of file" in repo_read_file(tmp_path, "f.txt", start_line=10)
    assert "before start_line" in repo_read_file(tmp_path, "f.txt", start_line=2, end_line=1)


def test_float_env_empty_string(monkeypatch):
    monkeypatch.setenv("AI_RATE_LIMIT", "")
    monkeypatch.setenv("REPO_CACHE_MAX_GB", "")
    cfg = Settings()
    assert cfg.ai_rate_limit == 2.0
    assert cfg.repo_cache_max_gb == 30.0


def test_redact_credentials_in_git_errors():
    from reviewer.repo_cache import _redact
    msg = "fatal: unable to access 'https://oauth2:glpat-SECRET@lab.x/p.git/'"
    assert "glpat-SECRET" not in _redact(msg)
    assert "https://***@lab.x" in _redact(msg)


def test_requirements_declare_runtime_deps():
    reqs = open("requirements.txt").read()
    for dep in ("anthropic", "httpx[socks]"):
        assert dep in reqs, f"{dep} missing from requirements.txt"


def test_release_modes_keep_vs_ephemeral(tmp_path, monkeypatch):
    """release() keeps the bare repo by default; REPO_CACHE_EPHEMERAL drops it."""
    import shutil as _shutil
    import subprocess as _sp
    if _shutil.which("git") is None:
        pytest.skip("git not available")
    from reviewer import repo_cache as rc

    def git(*args):
        _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                check=True, capture_output=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", str(origin))
    (origin / "f.txt").write_text("x")
    git("-C", str(origin), "add", ".")
    git("-C", str(origin), "commit", "-qm", "c1")

    repo_dir = tmp_path / "cache" / "host" / "grp" / "app.git"
    repo_dir.parent.mkdir(parents=True)
    git("clone", "-q", "--bare", str(origin), str(repo_dir))

    def add_worktree(name):
        wt = repo_dir.parent / name
        git("-C", str(repo_dir), "worktree", "add", "--detach", str(wt))
        return wt

    cache = rc.RepoCache(cache_dir=str(tmp_path / "cache"))

    # default mode: worktree removed, bare repo kept for the next MR
    monkeypatch.setattr(rc.settings, "repo_cache_ephemeral", False)
    wt1 = add_worktree("app-mr1-aaa-wt")
    asyncio.run(cache.release(wt1))
    assert not wt1.exists() and repo_dir.exists()

    # ephemeral mode: bare repo dropped too
    monkeypatch.setattr(rc.settings, "repo_cache_ephemeral", True)
    wt2 = add_worktree("app-mr2-bbb-wt")
    asyncio.run(cache.release(wt2))
    assert not wt2.exists() and not repo_dir.exists()


# --- server queue dedupe ---

def test_review_queue_dedupe():
    async def run():
        # burst_window=0 isolates sha-keyed dedupe (burst collapsing has its own test)
        queue = ReviewQueue(workers=0, dedupe_ttl=600, burst_window=0)
        mr = {"gitlab_config": {"name": "primary"}, "project_id": 1,
              "mr_iid": 7, "last_commit": "abc"}
        assert queue.submit(dict(mr)) is True
        assert queue.submit(dict(mr)) is False          # webhook retry
        assert queue.submit({**mr, "last_commit": "def"}) is True  # new push
    asyncio.run(run())
