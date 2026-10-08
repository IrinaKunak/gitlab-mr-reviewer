"""Offline unit tests for v2 (no network, no API keys needed).

Run: .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from reviewer import ai_client as ai_mod
from reviewer import bridge as bridge_mod
from reviewer import gitlab_io
from reviewer.ai_client import AIClient, extract_json
from reviewer.config import Settings
from reviewer.domain.models import ChangeSet, FileChange, InstanceRef, TriageResult
from reviewer.repo_cache import _safe_path, repo_grep, repo_list_tree, repo_read_file
from reviewer.server import ReviewQueue
from tests.factories import INSTANCE, dialogue_job, review_job

# --- config ---

def test_proxy_precedence(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://p:1")
    monkeypatch.setenv("SOCKS_PROXY", "s:2")
    cfg = Settings()
    assert cfg.network.proxy_url == "http://p:1"
    assert cfg.network.requests_proxies == {"http": "http://p:1", "https": "http://p:1"}
    monkeypatch.delenv("HTTP_PROXY")
    cfg = Settings()
    assert cfg.network.proxy_url == "socks5://s:2"
    assert cfg.network.requests_proxies["https"] == "socks5h://s:2"


def test_instances_routing_keyed_by_webhook_token(clean_env, monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "https://a")
    monkeypatch.setenv("GITLAB_TOKEN", "t1")
    monkeypatch.setenv("XGITLABTOKEN", "hook1")
    monkeypatch.setenv("GITLAB_URL_2", "https://b")
    monkeypatch.setenv("GITLAB_TOKEN_2", "t2")
    monkeypatch.setenv("XGITLABTOKEN_2", "hook2")
    cfg = Settings()
    assert cfg.gitlab.routes["hook1"].name == "primary"
    assert cfg.gitlab.routes["hook2"].url == "https://b"
    # stage 8: the numbered env trios still work, with a deprecation warning
    from reviewer.config import deprecated_env_vars_in_use
    [message] = deprecated_env_vars_in_use(cfg)
    assert "GITLAB_URL_2" in message and "config.yaml" in message and "IGNORED" not in message


def test_tier_mapping_and_fallback_chains():
    cfg = Settings()
    assert cfg.model_for_tier("fast") == cfg.llm.tiers.fast.model
    assert cfg.fallback_chain("smart")[0].startswith("anthropic/")


def test_openrouter_provider_skips_gateway(tmp_path):
    """AI_PROVIDER=openrouter routes a plain Claude id straight to OpenRouter."""
    cfg = Settings()
    cfg.llm.provider = "openrouter"
    cfg.storage.ai_cache_dir = str(tmp_path)
    cfg.llm.tiers.main.model = "claude-sonnet-5"
    cfg.llm.tiers.main.fallback = [
        "anthropic/claude-sonnet-5",
        "google/gemini-3.6-flash",
        "deepseek/deepseek-v4-pro",
    ]
    client = AIClient(cfg)
    seen: dict = {}

    async def fake_fallback(tier, system, messages, max_tokens, json_schema,
                            timeout, cause, chain=None):
        seen["chain"] = chain
        seen["primary_built"] = client._primary is not None
        return ai_mod.AIResult(text="ok", model=chain[0], provider="openrouter")

    client._fallback_complete = fake_fallback
    result = asyncio.run(client.complete("main", "sys", "diff", use_cache=False))
    assert result.provider == "openrouter"
    assert seen["chain"][0] == "anthropic/claude-sonnet-5"
    assert seen["primary_built"] is False
    # default provider still keeps plain Claude ids on the gateway
    assert ai_mod.uses_openrouter("anthropic", "claude-sonnet-5") is False
    assert ai_mod.uses_openrouter("anthropic", "openai/gpt-5.6-terra") is True


# --- ai_client helpers ---

def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('prose\n```json\n{"a": 1}\n```\nmore') == {"a": 1}
    assert extract_json('text {"a": {"b": 2}} tail') == {"a": {"b": 2}}
    assert extract_json("no json here") is None


def test_cache_roundtrip(tmp_path):
    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path)
    cfg.llm.cache_ttl = 3600
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
    cfg.llm.debug = True
    blocker = tmp_path / "blocker"
    blocker.write_text("")  # file where a directory is needed -> mkdir raises OSError
    cfg.storage.log_dir = str(blocker / "logs")
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
    cfg.storage.ai_cache_dir = str(tmp_path)
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
    cfg.llm.anthropic.api_url = "https://gateway.example/anthropic"
    cfg.llm.anthropic.api_key = "sk-ant-real"
    cfg.llm.anthropic.gateway_key = "cfut_abc123"
    primary = AIClient(cfg).primary
    assert primary.api_key == "sk-ant-real"
    assert primary.default_headers.get("cf-aig-authorization") == "Bearer cfut_abc123"

    # mode 2: cfut only (Unified Billing) -> header + dummy x-api-key
    cfg2 = Settings()
    cfg2.llm.anthropic.api_url = "https://gateway.example/anthropic"
    cfg2.llm.anthropic.api_key = ""
    cfg2.llm.anthropic.gateway_key = "cfut_abc123"
    primary2 = AIClient(cfg2).primary
    assert primary2.api_key == "gateway"
    assert primary2.default_headers.get("cf-aig-authorization") == "Bearer cfut_abc123"

    # mode 3 / legacy: real key stored in the GATEWAY var -> plain x-api-key
    cfg3 = Settings()
    cfg3.llm.anthropic.api_url = "https://gateway.example/anthropic"
    cfg3.llm.anthropic.api_key = ""
    cfg3.llm.anthropic.gateway_key = "sk-ant-real"
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
    # claude-opus-5 rejects disabled thinking at effort xhigh/max — effort must
    # never be emitted alongside it, whatever the caller passes
    for eff in (None, "high", "xhigh", "max"):
        assert "output_config" not in client._primary_params("main", eff, "claude-opus-5")
    # smart tier keeps adaptive + effort, which opus-5 accepts
    assert client._primary_params("smart", "high", "claude-opus-5") == {
        "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}


def test_primary_params_models_without_disabled_thinking():
    # prod 2026-09-29 (!127): main switched to claude-sonnet-5-5 -> 400
    # '"thinking.type.disabled" is not supported for this model'
    client = AIClient(Settings())
    for eff in (None, "high", "xhigh", "max"):
        # between_tools takes no other field and 400s at effort xhigh/max
        assert client._primary_params("main", eff, "claude-sonnet-5-5") == {
            "thinking": {"type": "between_tools"}}
    # no thinking-off mode at all: adaptive at the lowest effort
    for model in ("claude-opus-5-5", "claude-fable-5-1"):
        assert client._primary_params("main", None, model) == {
            "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
    # older main models keep the explicit disable
    assert client._primary_params("main", None, "claude-sonnet-5") == {
        "thinking": {"type": "disabled"}}
    assert client._primary_params("smart", "high", "claude-sonnet-5-5") == {
        "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}


def test_openrouter_models_array_capped():
    # prod !779: smart overridden to openai/gpt-5.6-terra + a 3-entry fallback
    # chain -> 4 models -> OpenRouter 400 "'models' array must have 3 items or
    # fewer" -> the whole investigation was lost after the review had run
    from reviewer.ai_client import MAX_OPENROUTER_MODELS, _routing_chain

    chain = ["anthropic/claude-opus-5", "google/gemini-3.6-flash",
             "deepseek/deepseek-v4-pro"]
    out = _routing_chain("openai/gpt-5.6-terra", chain)
    assert len(out) == MAX_OPENROUTER_MODELS == 3
    assert out[0] == "openai/gpt-5.6-terra"          # override is preferred
    # an override already in the chain is not duplicated
    assert _routing_chain("anthropic/claude-opus-5", chain) == [
        "anthropic/claude-opus-5", "google/gemini-3.6-flash",
        "deepseek/deepseek-v4-pro"]
    # a long configured chain is capped too
    assert len(_routing_chain("", chain + ["a/b", "c/d"])) == 3


def test_token_estimate_matches_measured_diff_ratio():
    # prod !779: 580k chars of diff billed 290,883 input tokens (2.0 chars/tok).
    # The old //3 estimate said ~193k — budgets silently admitted ~50% more
    # than intended, so a "under budget" review really cost 291k tokens.
    from reviewer.ai_client import CHARS_PER_TOKEN, estimate_tokens

    assert CHARS_PER_TOKEN == 2
    assert estimate_tokens("x" * 580_000) >= 290_000


def test_input_size_guard():
    cfg = Settings()
    cfg.llm.max_input_tokens = 10
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
    monkeypatch.setattr(usage.settings.storage, "log_dir", str(tmp_path))
    job = review_job(project_path="g/p", mr_iid=7)
    usage.persist(tracker, job)
    usage.persist(tracker, job)
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

    monkeypatch.setattr(settings.storage, "state_dir", str(tmp_path))

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
    openrouter_models._store.write((parsed, 1e18))  # never stale
    assert usage.price_of("z-ai/glm-5") == (0.6, 2.2)
    cost = usage.cost_usd("z-ai/glm-5", 1_000_000, 1_000_000)
    assert abs(cost - (0.6 + 2.2)) < 1e-9
    # curated prices still win over the catalog
    assert usage.price_of("claude-opus-4-8") == (5.0, 25.0)
    # opus-5 (released 2026-07-24) is priced like 4.8 on both routes — an
    # unpriced tier model would silently cost $0 in the stats
    assert usage.price_of("claude-opus-5") == (5.0, 25.0)
    assert usage.price_of("anthropic/claude-opus-5") == (5.0, 25.0)
    assert usage.price_of(Settings().llm.tiers.smart.model) != (0.0, 0.0)
    # a genuinely unknown model is $0 (fail-open), not a crash
    assert usage.price_of("totally/unknown") == (0.0, 0.0)

    # refresh() is fail-open: a network error keeps the cached copy
    def _boom():
        raise RuntimeError("no network")
    monkeypatch.setattr(openrouter_models, "_fetch_now", _boom)
    openrouter_models._store.write((parsed, 0.0))  # stale -> forces a refresh attempt
    assert openrouter_models.refresh() == parsed  # falls back to cache, no raise


def test_agent_loop_marks_prompt_cache_breakpoints(tmp_path, monkeypatch):
    # Anthropic caching is opt-in: without cache_control every investigator
    # iteration re-bills the whole repo/diff prefix (that is why the same loop
    # cost ~2x more on the CF gateway than on auto-caching OpenRouter models)
    import asyncio
    from types import SimpleNamespace

    from reviewer.ai_client import ToolDef

    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path)
    cfg.llm.rate_limit = 0
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


def test_openrouter_agent_loop_keeps_cache_control_for_claude(tmp_path, monkeypatch):
    # regression: AI_PROVIDER=openrouter stripped cache_control everywhere, but
    # Claude does not auto-cache on OpenRouter — !493 billed 1.57M input tokens
    # with 0 cached ($6.05). anthropic/* keeps the markers; others still strip.
    import asyncio
    from types import SimpleNamespace

    from reviewer.ai_client import ToolDef

    def run(head_model):
        cfg = Settings()
        cfg.llm.provider = "openrouter"
        cfg.storage.ai_cache_dir = str(tmp_path)
        cfg.llm.rate_limit = 0
        cfg.llm.tiers.smart.model = "claude-opus-5"
        cfg.llm.tiers.smart.fallback = [head_model, "moonshotai/kimi-k3"]
        monkeypatch.setattr(ai_mod.overrides, "model_for_tier",
                            lambda tier, c: c.model_for_tier(tier))
        client = AIClient(cfg)
        sent: list[dict] = []
        turns = iter(["tool_use", "end_turn"])

        async def fake_create(**kwargs):
            sent.append(kwargs)
            if next(turns) == "tool_use":
                return SimpleNamespace(
                    stop_reason="tool_use", model=head_model,
                    content=[SimpleNamespace(type="tool_use", id="t1", name="t", input={})],
                    usage=SimpleNamespace(input_tokens=5, output_tokens=5))
            return SimpleNamespace(
                stop_reason="end_turn", model=head_model,
                content=[SimpleNamespace(type="text", text="done")],
                usage=SimpleNamespace(input_tokens=5, output_tokens=5))

        stub = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
        stub.with_options = lambda **kw: stub
        client._fallback = stub

        async def handler(**kw):
            return "tool output"

        asyncio.run(client.agent_loop(
            "smart", "sys", "big diff", [ToolDef("t", "d", {}, handler)],
            max_iterations=5))
        return sent

    def breakpoints(messages):
        return [b for m in messages for b in m["content"]
                if isinstance(b, dict) and "cache_control" in b]

    claude = run("anthropic/claude-sonnet-5.5")
    assert all(req["model"] == "anthropic/claude-sonnet-5.5" for req in claude)
    assert claude[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert len(breakpoints(claude[-1]["messages"])) == 2  # prefix + rolling turn

    other = run("openai/gpt-5.6-terra")
    assert all(breakpoints(req["messages"]) == [] for req in other)
    assert ai_mod.wants_cache_control(False, "claude-sonnet-5-5") is True


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
    cfg.storage.ai_cache_dir = str(tmp_path)
    cfg.llm.rate_limit = 0
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

    monkeypatch.setattr(live_settings.storage, "state_dir", str(tmp_path))

    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path)
    assert overrides.model_for_tier("smart", cfg) == cfg.llm.tiers.smart.model  # env default
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


def test_basic_auth(monkeypatch):
    import base64

    from reviewer.config import settings
    from reviewer.server import basic_auth_ok, stats_access_allowed

    monkeypatch.setattr(settings.server, "stats_user", "max")
    monkeypatch.setattr(settings.server, "stats_password", "pw123")
    good = "Basic " + base64.b64encode(b"max:pw123").decode()
    bad = "Basic " + base64.b64encode(b"max:nope").decode()
    assert basic_auth_ok(good) is True
    assert basic_auth_ok(bad) is False
    assert basic_auth_ok("Bearer xyz") is False
    # with basic configured, unauthenticated local access is no longer allowed
    monkeypatch.setattr(settings.server, "stats_token", "")
    assert stats_access_allowed(good, "", "203.0.113.7") is True
    assert stats_access_allowed("", "", None) is False


def test_stats_access_control(monkeypatch):
    from reviewer.config import settings
    from reviewer.server import stats_access_allowed

    # no token configured: only direct (non-proxied) requests pass
    monkeypatch.setattr(settings.server, "stats_token", "")
    assert stats_access_allowed("", "", None) is True
    assert stats_access_allowed("", "", "203.0.113.7") is False

    # token configured: Bearer header or ?token= must match exactly
    monkeypatch.setattr(settings.server, "stats_token", "s3cret")
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
    cfg.storage.ai_cache_dir = str(tmp_path)
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

    monkeypatch.setattr(settings.bridge, "chat_id", "-100bridge")
    monkeypatch.setattr(settings.notify.telegram, "enabled", True)
    monkeypatch.setattr(settings.notify.telegram, "chat_ids", ["-100team", "-100extra"])
    monkeypatch.setattr(settings.notify.telegram, "tester_report_chat_ids", [])
    assert tester_report_targets() == ["-100bridge", "-100team", "-100extra"]

    # explicit override narrows the team targets; dedupe against bridge
    monkeypatch.setattr(settings.notify.telegram, "tester_report_chat_ids", ["-100team", "-100bridge"])
    assert tester_report_targets() == ["-100bridge", "-100team"]

    # telegram off -> only the bridge copy
    monkeypatch.setattr(settings.notify.telegram, "enabled", False)
    assert tester_report_targets() == ["-100bridge"]


def test_translate_guard_rejects_non_cyrillic_output(monkeypatch):
    # regression: Haiku answered the translate request with English commentary
    # ("you haven't provided a markdown document") and it was posted as the review
    import asyncio
    from types import SimpleNamespace

    from reviewer.config import settings
    from reviewer.pipeline import Pipeline

    monkeypatch.setattr(settings.pipeline, "language", "ru")
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
        asyncio.run(p._process_inner(review_job(), {}))


def test_review_state_roundtrip_and_bound(tmp_path, monkeypatch):
    from reviewer import review_state
    from reviewer.config import settings

    monkeypatch.setattr(settings.storage, "state_dir", str(tmp_path))

    assert review_state.get_last_sha("primary", 1, 2) is None
    review_state.set_last_sha("primary", 1, 2, "abc123")
    assert review_state.get_last_sha("primary", 1, 2) == "abc123"
    review_state.set_last_sha("primary", 1, 2, "def456")  # newer push wins
    assert review_state.get_last_sha("primary", 1, 2) == "def456"

    # survives a cold start (persisted to the cache volume)
    review_state._store.invalidate()
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
    assert "No hypotheticals" in prompts.REVIEW_SYSTEM
    # dev feedback 2026-07-30 (Irina, telemarketing-back !8/!9): four findings in
    # a row were "confirm that a periodic reconciliation exists", about code the
    # reviewer was never shown. Banning second-guessing the author's DECISIONS
    # did not cover asking whether something exists ELSEWHERE — the reviewer
    # cannot see the rest of the repo, so those must be dropped, not hedged.
    assert "ONLY this merge request's changes" in prompts.REVIEW_SYSTEM
    assert "never ask whether such a thing" in prompts.REVIEW_SYSTEM
    for banned in ('"confirm"', '"verify"', '"make sure"', '"double-check"'):
        assert banned in prompts.REVIEW_SYSTEM, banned
    # a docstring explaining WHY is the answer; don't re-ask it
    assert "is the author's" in prompts.REVIEW_SYSTEM
    # and don't ship a finding you yourself called fine
    assert '"looks correct"' in prompts.REVIEW_SYSTEM
    # the investigator DOES have the repo — it must check, not ask
    assert "grep for it and report what you found" in prompts.INVESTIGATOR_SYSTEM
    note = prompts.INCREMENTAL_REVIEW_NOTE.format(prev_sha="abc12345")
    assert "abc12345" in note and "delta" in note
    assert ".ai-review.md" in prompts.guidelines_section("Focus on SQL")

    # delta fetch turns compare diffs into a ChangeSet; degrades to None
    stub = SimpleNamespace(
        repository_compare=lambda a, b: {"diffs": [{"new_path": "x.py", "diff": "+1"}]})
    delta = gitlab_io.fetch_delta_changes(stub, "aaa", "bbb")
    assert delta == ChangeSet((FileChange(old_path="", new_path="x.py", diff="+1"),))
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

    changes = gitlab_io.to_changeset({"changes": [
        {"new_path": "src/auth.py", "diff": "+def login():\n" * 50},
        {"new_path": "public/logo.svg", "diff": "+<path d='M0 0'/>\n" * 400},
        {"new_path": "yarn.lock", "diff": "+dep\n" * 300, "new_file": True},
        {"new_path": "src/pay.py", "diff": "+def charge():\n" * 50},
    ]})

    # the manifest triage judges from: status, size and path for every file
    manifest = gitlab_io.file_manifest(changes)
    assert "modified\t" in manifest and "public/logo.svg" in manifest
    assert "added\t" in manifest                     # yarn.lock is new_file
    assert manifest in prompts.triage_user_prompt(review_job(), "diff", manifest)
    assert "skip_globs" in prompts.TRIAGE_SCHEMA["properties"]
    assert "skip_globs" in prompts.TRIAGE_SYSTEM

    # (resolve_skip's pattern rules and guards: tests/test_domain.py)
    skip = {"public/logo.svg", "yarn.lock"}

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

    monkeypatch.setattr(settings.llm, "max_input_tokens", 10_000)
    p = Pipeline(client=AIClient(Settings()))
    monkeypatch.setattr(p.ai.cfg.llm, "max_input_tokens", 10_000)
    triage, mr_data = TriageResult(summary="s"), review_job(mr_iid=779)

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
    parsed = gitlab_io.parse_merge_request_webhook(payload, INSTANCE)
    assert parsed and parsed.force_full is True
    payload["labels"] = []
    assert gitlab_io.parse_merge_request_webhook(payload, INSTANCE).force_full is False
    payload["object_attributes"]["title"] = "INCR-54 [re-review]"
    assert gitlab_io.parse_merge_request_webhook(payload, INSTANCE).force_full is True

    q = ReviewQueue(workers=1, dedupe_ttl=600, burst_window=30)
    job = review_job(project_id=170, mr_iid=5, last_commit="abc")
    assert q.submit(job) is True
    assert q.submit(job) is False                                # normal dedupe
    assert q.submit(replace(job, force_full=True)) is True       # forced through


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

    monkeypatch.setattr(settings.pipeline, "language", "ru")
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
    asyncio.run(p._process_inner(review_job(last_commit="abc123"), {}))


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
    changes = gitlab_io.to_changeset({"changes": [
        {"new_path": "reviewer/ai_client.py", "diff": "", "collapsed": True, "new_file": True},
        {"new_path": "small.py", "diff": "+ok", "new_file": True},
        {"new_path": "unchanged.py", "diff": ""},  # genuinely empty -> still skipped
    ]})
    out = gitlab_io.extract_review_content(project, mr, changes)
    assert "reviewer/ai_client.py" in out and "def core" in out
    assert "DIFF UNAVAILABLE" in out
    assert "unchanged.py" not in out

    diff_only = gitlab_io.extract_diff_only(changes)
    assert "[diff unavailable: file too large]" in diff_only


def test_parse_webhook_url_fix_and_actions():
    parsed = gitlab_io.parse_merge_request_webhook(WEBHOOK_PAYLOAD, INSTANCE)
    assert parsed is not None
    assert parsed.ref.url == "https://lab/x/-/merge_requests/7"  # contractual URL typo fix
    assert parsed.last_commit == "deadbeef"
    assert parsed.ref.instance is INSTANCE

    closed = {**WEBHOOK_PAYLOAD,
              "object_attributes": {**WEBHOOK_PAYLOAD["object_attributes"], "action": "close"}}
    assert gitlab_io.parse_merge_request_webhook(closed, INSTANCE) is None


def test_parse_webhook_no_review_marker():
    tagged = {**WEBHOOK_PAYLOAD,
              "object_attributes": {**WEBHOOK_PAYLOAD["object_attributes"],
                                    "title": "big infra change [no-review]"}}
    assert gitlab_io.parse_merge_request_webhook(tagged, INSTANCE) is None

    labeled = {**WEBHOOK_PAYLOAD, "labels": [{"title": "No-Review"}]}
    assert gitlab_io.parse_merge_request_webhook(labeled, INSTANCE) is None


def test_extract_jira_keys():
    parsed = gitlab_io.parse_merge_request_webhook(WEBHOOK_PAYLOAD, INSTANCE)
    keys = gitlab_io.extract_jira_keys(parsed)
    assert keys == ["PBV-123", "ABC-9"]


def test_format_review_comment_language(monkeypatch):
    from reviewer.config import settings
    monkeypatch.setattr(settings.pipeline, "language", "ru")
    comment = gitlab_io.format_review_comment("текст обзора")
    assert "Автоматический обзор кода" in comment
    monkeypatch.setattr(settings.pipeline, "language", "en")
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
    assert cfg.llm.rate_limit == 2.0
    assert cfg.repo_cache.max_gb == 30.0


def test_redact_credentials_in_git_errors():
    from reviewer.repo_cache import _redact
    msg = "fatal: unable to access 'https://oauth2:glpat-SECRET@lab.x/p.git/'"
    assert "glpat-SECRET" not in _redact(msg)
    assert "https://***@lab.x" in _redact(msg)


def test_pyproject_declares_runtime_deps():
    import tomllib

    with open("pyproject.toml", "rb") as fh:
        deps = " ".join(tomllib.load(fh)["project"]["dependencies"])
    for dep in ("anthropic", "httpx[socks]", "fastapi", "python-gitlab", "uvicorn",
                "requests"):
        assert dep in deps, f"{dep} missing from pyproject.toml"


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
    monkeypatch.setattr(rc.settings.repo_cache, "ephemeral", False)
    wt1 = add_worktree("app-mr1-aaa-wt")
    asyncio.run(cache.release(wt1))
    assert not wt1.exists() and repo_dir.exists()

    # ephemeral mode: bare repo dropped too
    monkeypatch.setattr(rc.settings.repo_cache, "ephemeral", True)
    wt2 = add_worktree("app-mr2-bbb-wt")
    asyncio.run(cache.release(wt2))
    assert not wt2.exists() and not repo_dir.exists()


# --- server queue dedupe ---

# --- MR dialogue & tool-assisted review (dev feedback 2026-07-31) ---

def test_parse_note_webhook_variants():
    # "жаль, он диалоги не поддерживает" — replies to the bot's review comment
    # arrive as Note Hook events; only human MR comments become dialogue jobs
    base = {
        "object_kind": "note",
        "user": {"username": "irina"},
        "project": {"id": 42, "path_with_namespace": "novacard/telemarketing-back"},
        "object_attributes": {
            "id": 555, "note": "а точно IsAuthenticated остался?",
            "noteable_type": "MergeRequest", "system": False,
            "discussion_id": "abc123", "position": None,
        },
        "merge_request": {"iid": 10, "url": "https://x/mr/10",
                          "last_commit": {"id": "sha1"}},
    }
    parsed = gitlab_io.parse_note_webhook(base, INSTANCE)
    assert parsed and parsed.kind == "dialogue"
    assert parsed.ref.mr_iid == 10 and parsed.note_id == 555
    assert parsed.discussion_id == "abc123"
    assert parsed.note_author == "irina"
    assert parsed.last_commit == "sha1"

    # a comment on a diff line carries its anchor
    diff_note = {**base, "object_attributes": {
        **base["object_attributes"],
        "position": {"new_path": "app/views.py", "new_line": 88}}}
    assert gitlab_io.parse_note_webhook(diff_note, INSTANCE).note_position == "app/views.py:88"

    # system notes, non-MR comments, empty bodies -> not dialogue material
    system_note = {**base, "object_attributes": {**base["object_attributes"], "system": True}}
    assert gitlab_io.parse_note_webhook(system_note, INSTANCE) is None
    issue_note = {**base, "object_attributes": {
        **base["object_attributes"], "noteable_type": "Issue"}}
    assert gitlab_io.parse_note_webhook(issue_note, INSTANCE) is None
    empty = {**base, "object_attributes": {**base["object_attributes"], "note": "  "}}
    assert gitlab_io.parse_note_webhook(empty, INSTANCE) is None
    assert gitlab_io.parse_note_webhook({**base, "merge_request": {}}, INSTANCE) is None
    assert gitlab_io.parse_note_webhook({"object_kind": "push"}, INSTANCE) is None


def test_thread_helpers():
    notes = [
        {"id": 1, "author": {"username": "reviewer-bot"}, "body": "## Review\nfinding A"},
        {"id": 2, "author": {"username": "irina"}, "body": "ну нет изменений же"},
        {"id": 3, "author": {"username": "gitlab"}, "system": True, "body": "added 1 commit"},
    ]
    text = gitlab_io.render_thread(notes, "reviewer-bot")
    assert "[@reviewer-bot [bot — this is you]]" in text
    assert "[@irina]" in text and "added 1 commit" not in text

    assert gitlab_io.thread_involves_bot(notes, "reviewer-bot") is True
    assert gitlab_io.thread_involves_bot(notes, "other-bot") is False
    assert gitlab_io.thread_involves_bot(notes, "") is False
    # bot note id 1 < trigger id 2 -> not answered yet; a bot note after -> answered
    assert gitlab_io.bot_answered_after(notes, 2, "reviewer-bot") is False
    answered = notes + [{"id": 4, "author": {"username": "reviewer-bot"}, "body": "ok"}]
    assert gitlab_io.bot_answered_after(answered, 2, "reviewer-bot") is True

    assert gitlab_io.mentions_user("cc @Reviewer-Bot, взгляни", "reviewer-bot") is True
    assert gitlab_io.mentions_user("no mention here", "reviewer-bot") is False
    assert gitlab_io.mentions_user("@reviewer-bot2 hi", "reviewer-bot") is False
    assert gitlab_io.mentions_user("hi", "") is False

    # discussion_context: hint path, scan fallback, API failure -> ("", [])
    from types import SimpleNamespace
    hit = SimpleNamespace(id="d9", attributes={"notes": [{"id": 5, "body": "x"}]})

    class _Discussions:
        def get(self, did):
            assert did == "d9"
            return hit
        def list(self, **kw):
            return iter([SimpleNamespace(id="other", attributes={"notes": [{"id": 1}]}),
                         hit])
    mr = SimpleNamespace(discussions=_Discussions())
    assert gitlab_io.discussion_context(mr, 5, "d9") == ("d9", [{"id": 5, "body": "x"}])
    assert gitlab_io.discussion_context(mr, 5, "") == ("d9", [{"id": 5, "body": "x"}])

    class _Broken:
        def get(self, did):
            raise RuntimeError("403")
        def list(self, **kw):
            raise RuntimeError("403")
    assert gitlab_io.discussion_context(
        SimpleNamespace(discussions=_Broken()), 5, "d9") == ("", [])


def test_dialogue_answers_in_thread(monkeypatch):
    # "Пусть сам подтверждает" — the bot answers a dev's reply, checking the
    # repo itself; NO_REPLY suppresses the answer; budget caps runaway threads
    from types import SimpleNamespace

    from reviewer import pipeline as pipeline_mod
    from reviewer.ai_client import AIResult
    from reviewer.config import settings

    monkeypatch.setattr(settings.pipeline, "language", "en")
    posted: list[tuple] = []

    stub_mr = SimpleNamespace(
        title="MR 10", source_branch="f", target_branch="dev", author={"username": "dev1"},
        changes=lambda access_raw_diffs=None: {"changes": [
            {"new_path": "a.py", "diff": "+x = 1"}]})
    stub_project = SimpleNamespace(mergerequests=SimpleNamespace(get=lambda iid: stub_mr))
    stub_gl = SimpleNamespace(user=SimpleNamespace(username="reviewer-bot"),
                              projects=SimpleNamespace(get=lambda pid: stub_project))
    monkeypatch.setattr(pipeline_mod.gitlab_io, "get_gitlab_client", lambda cfg: stub_gl)
    thread = [{"id": 1, "author": {"username": "reviewer-bot"}, "body": "finding"},
              {"id": 2, "author": {"username": "irina"}, "body": "точно?"}]
    monkeypatch.setattr(pipeline_mod.gitlab_io, "discussion_context",
                        lambda mr, nid, did="": ("d1", thread))

    async def _no_repo(*a, **kw):
        raise RuntimeError("clone disabled in tests")
    monkeypatch.setattr(pipeline_mod.repo_cache, "checkout_mr", _no_repo)

    async def _record_reply(mr, did, body):
        posted.append(("thread", did, body))
    monkeypatch.setattr(pipeline_mod.gitlab_io, "post_discussion_reply", _record_reply)

    async def _record_note(mr, body):
        posted.append(("note", body))
    monkeypatch.setattr(pipeline_mod.gitlab_io, "post_note", _record_note)

    answers = iter([AIResult(text="Checked views.py:12 — IsAuthenticated is intact."),
                    AIResult(text="NO_REPLY")])
    seen_prompts: list[str] = []

    class StubAI:
        async def agent_loop(self, tier, system, user, tools, **kw):
            assert tier == "main"
            seen_prompts.append(user)
            return next(answers)

    p = pipeline_mod.Pipeline(client=StubAI())
    note = dialogue_job(project_id=1, project_path="g/p", mr_iid=10, note_id=2,
                        discussion_id="d1", note_body="точно?", note_author="irina",
                        last_commit="sha1")
    asyncio.run(p.process_note(note))
    assert posted == [("thread", "d1", "Checked views.py:12 — IsAuthenticated is intact.")]
    # the model sees the thread, knows which side it is, and the diff
    assert "[bot — this is you]" in seen_prompts[0]
    assert "Answer the last message, from @irina." in seen_prompts[0]
    assert "+x = 1" in seen_prompts[0]

    # NO_REPLY -> nothing posted
    asyncio.run(p.process_note(replace(note, note_id=3)))
    assert len(posted) == 1

    # the bot's own note must never trigger an answer (loop guard)
    asyncio.run(p.process_note(replace(note, note_id=4, note_author="reviewer-bot")))
    assert len(posted) == 1

    # a thread without the bot and without a mention is the humans talking
    monkeypatch.setattr(pipeline_mod.gitlab_io, "discussion_context",
                        lambda mr, nid, did="": ("d2", [
                            {"id": 9, "author": {"username": "artem"}, "body": "hi"}]))
    asyncio.run(p.process_note(replace(note, note_id=9, discussion_id="d2")))
    assert len(posted) == 1

    # per-MR budget: once exhausted the bot stays silent
    monkeypatch.setattr(settings.pipeline, "dialogue_max_replies_per_mr", 1)
    assert p._dialogue_budget_ok(("primary", 1, 10)) is False
    assert p._dialogue_budget_ok(("primary", 1, 11)) is True


def test_review_with_tools_verifies_and_falls_back(monkeypatch):
    # the review stage checks its own cross-file concerns with repo tools;
    # any tool-path failure degrades to the plain single-shot review
    from reviewer import pipeline as pipeline_mod
    from reviewer.ai_client import AIError, AIResult

    mr_data = review_job(mr_iid=1, author="dev1", source_branch="f", target_branch="dev")
    triage = TriageResult()
    calls: list[str] = []

    class StubAI:
        def __init__(self, agent_result=None, agent_exc=None):
            self.agent_result, self.agent_exc = agent_result, agent_exc

        async def agent_loop(self, tier, system, user, tools, **kw):
            calls.append("agent")
            assert tier == "main"
            assert [t.name for t in tools] == ["repo_find_symbol", "repo_grep",
                                               "repo_read_file", "repo_list_tree"]
            assert "REPO ACCESS FOR THIS REVIEW" in system
            if self.agent_exc:
                raise self.agent_exc
            return self.agent_result

        async def complete(self, tier, system, user, **kw):
            calls.append("complete")
            return AIResult(text="## Verdict\n**SHIP** plain path")

    # tool path succeeds -> plain completion never runs
    p = pipeline_mod.Pipeline(
        client=StubAI(agent_result=AIResult(text="## Verdict\n**SHIP** verified")))
    out = asyncio.run(p._review(mr_data, "content", triage, "diff", "", None,
                                worktree=object()))
    assert out.text == "## Verdict\n**SHIP** verified" and calls == ["agent"]
    assert out.tool_assisted

    # loop dies (refusal, provider trouble) -> plain review still ships
    calls.clear()
    p = pipeline_mod.Pipeline(client=StubAI(agent_exc=AIError("boom")))
    out = asyncio.run(p._review(mr_data, "content", triage, "diff", "", None,
                                worktree=object()))
    assert "plain path" in out.text and calls == ["agent", "complete"]

    # loop ran out of turns mid-check (no verdict) -> plain review
    calls.clear()
    p = pipeline_mod.Pipeline(client=StubAI(agent_result=AIResult(text="hmm, checking")))
    out = asyncio.run(p._review(mr_data, "content", triage, "diff", "", None,
                                worktree=object()))
    assert "plain path" in out.text and calls == ["agent", "complete"]

    # no worktree (checkout failed / flag off) -> straight to the plain path
    calls.clear()
    p = pipeline_mod.Pipeline(client=StubAI())
    out = asyncio.run(p._review(mr_data, "content", triage, "diff", "", None,
                                worktree=None))
    assert calls == ["complete"]


def test_dialogue_and_tools_prompt_contract():
    from reviewer import prompts

    note = prompts.REVIEW_TOOLS_NOTE.format(max_calls=8)
    assert "REPO ACCESS FOR THIS REVIEW" in note
    assert "about 8 tool calls" in note
    # the upgrade of the epistemic rule: unverifiable -> CHECK it, not drop it
    assert "CHECK it yourself" in note
    assert "confirm what these tools can" in note

    assert "NO_REPLY" in prompts.DIALOGUE_SYSTEM
    assert "CHECK, don't ask" in prompts.DIALOGUE_SYSTEM
    assert "cannot approve, merge, or modify" in prompts.DIALOGUE_SYSTEM
    user = prompts.dialogue_user_prompt("hdr", "thread-text", "irina",
                                        position="a.py:5", diff="+d")
    assert "thread-text" in user and "a.py:5" in user
    assert user.rstrip().endswith("Answer the last message, from @irina.")


def test_dialogue_and_review_tools_flags(monkeypatch):
    for var in ("REVIEW_REPO_TOOLS", "MR_DIALOGUE", "REVIEW_MAX_TOOL_CALLS",
                "DIALOGUE_MAX_REPLIES_PER_MR"):
        monkeypatch.delenv(var, raising=False)
    cfg = Settings()
    assert cfg.pipeline.stages.review_repo_tools is True and cfg.pipeline.stages.dialogue is True
    assert cfg.pipeline.review_max_tool_calls == 8
    assert cfg.pipeline.dialogue_max_replies_per_mr == 20
    monkeypatch.setenv("REVIEW_REPO_TOOLS", "off")
    monkeypatch.setenv("MR_DIALOGUE", "off")
    cfg = Settings()
    assert cfg.pipeline.stages.review_repo_tools is False and cfg.pipeline.stages.dialogue is False


# --- repo tool engines: ripgrep + ctags symbol index ---

def test_grep_python_engine_directly(tmp_path):
    # the fallback engine must keep working even where rg is installed
    from reviewer import repo_cache as rc

    (tmp_path / "a.py").write_text("def reconcile():\n    pass\n")
    hits = rc._grep_python(tmp_path, r"reconcile")
    assert "a.py:1" in hits
    assert rc._grep_python(tmp_path, r"nothing_here") == "No matches."
    assert "Invalid regex" in rc._grep_python(tmp_path, r"([")


@pytest.mark.skipif(__import__("shutil").which("rg") is None,
                    reason="ripgrep not installed")
def test_grep_ripgrep_engine(tmp_path):
    from reviewer import repo_cache as rc

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "views.py").write_text(
        "class LeadHistoryView:\n    permission_classes = [IsAuthenticated]\n")
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [reconcile]\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("IsAuthenticated noise\n")

    hits = rc.repo_grep(tmp_path, r"IsAuthenticated")
    assert "src/views.py:2" in hits
    assert "node_modules" not in hits            # SKIP_DIRS respected
    # dotfiles are searched (the Python engine always did — parity matters:
    # CI configs live in dotfiles)
    assert ".gitlab-ci.yml:1" in rc.repo_grep(tmp_path, r"reconcile")
    # glob filter narrows the search
    assert "views.py" not in rc.repo_grep(tmp_path, r"reconcile", glob="*.py")
    # lookahead is not Rust-regex: rg exits 2 and the Python engine takes over
    assert "src/views.py:2" in rc.repo_grep(tmp_path, r"IsAuthenticated(?=\])")
    assert rc.repo_grep(tmp_path, r"absent_symbol_xyz") == "No matches."


def test_symbol_index_lookup(tmp_path, monkeypatch):
    # repo_find_symbol answers "where is X defined" in ONE tool call — grep
    # made the model guess file locations and burn its verification budget
    from reviewer import repo_cache as rc

    canned = "\n".join([
        '{"_type": "tag", "name": "LeadSerializer", "path": "app/serializers.py", '
        '"line": 14, "kind": "class"}',
        '{"_type": "tag", "name": "LeadHistoryView", "path": "app/views.py", '
        '"line": 88, "kind": "class"}',
        '{"_type": "tag", "name": "lead_reconcile_task", "path": "app/tasks.py", '
        '"line": 5, "kind": "function"}',
        '{"_type": "ptag", "name": "!_TAG_PROGRAM"}',   # pseudo-tags are skipped
        "not-json-garbage",
    ])
    calls = {"n": 0}

    def fake_ctags(worktree):
        calls["n"] += 1
        return canned

    monkeypatch.setattr(rc, "_run_ctags", fake_ctags)
    rc.clear_symbol_index(tmp_path)

    out = rc.repo_find_symbol(tmp_path, "LeadSerializer")
    assert "app/serializers.py:14" in out and "class" in out
    # case-insensitive and substring fallbacks
    assert "app/views.py:88" in rc.repo_find_symbol(tmp_path, "leadhistoryview")
    assert "app/tasks.py:5" in rc.repo_find_symbol(tmp_path, "reconcile")
    assert "Try repo_grep" in rc.repo_find_symbol(tmp_path, "NoSuchThing")
    assert calls["n"] == 1                       # index built once, then cached
    rc.clear_symbol_index(tmp_path)
    rc.repo_find_symbol(tmp_path, "LeadSerializer")
    assert calls["n"] == 2                       # release() invalidates

    # no ctags in the deployment -> the tool says so and points at grep
    monkeypatch.setattr(rc, "_run_ctags", lambda wt: None)
    rc.clear_symbol_index(tmp_path)
    assert "unavailable" in rc.repo_find_symbol(tmp_path, "LeadSerializer")
    rc.clear_symbol_index(tmp_path)


def test_cache_sweep_spares_state_files(tmp_path):
    # regression (prod 2026-10): the 48h AI-cache sweep shared cache/ with the
    # state files and deleted model_overrides.json / reviewed_shas.json, so
    # dashboard overrides silently reverted on the next restart
    import os
    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path)
    client = AIClient(cfg)
    old = time.time() - 72 * 3600
    stale_entry = tmp_path / ("a" * 64)
    fresh_entry = tmp_path / ("b" * 64)
    state_file = tmp_path / "model_overrides.json"
    for path in (stale_entry, fresh_entry, state_file):
        path.write_text("x", encoding="utf-8")
    for path in (stale_entry, state_file):
        os.utime(path, (old, old))
    client._cleanup_cache()
    assert not stale_entry.exists()  # expired cache entry swept
    assert fresh_entry.exists()
    assert state_file.exists()  # non-cache files are never touched


def test_state_files_live_outside_ai_cache_dir(tmp_path, monkeypatch):
    from reviewer import openrouter_models, overrides, review_state
    from reviewer.config import settings
    monkeypatch.setattr(settings.storage, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings.storage, "ai_cache_dir", str(tmp_path / "cache" / "ai"))
    for mod in (overrides, review_state, openrouter_models):
        assert mod._store.path.parent == tmp_path / "state"
    assert Settings().storage.ai_cache_dir != Settings().storage.state_dir


def test_state_migration_moves_legacy_files(tmp_path):
    from reviewer import state_layout
    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path / "cache" / "ai")
    cfg.storage.state_dir = str(tmp_path / "state")
    legacy = tmp_path / "cache"
    legacy.mkdir()
    (legacy / "model_overrides.json").write_text('{"smart": "openai/x"}', encoding="utf-8")
    (legacy / "reviewed_shas.json").write_text('{"k": "old"}', encoding="utf-8")
    (legacy / ("c" * 64)).write_text("orphaned cache entry", encoding="utf-8")
    (legacy / "notes.txt").write_text("unrelated", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "reviewed_shas.json").write_text('{"k": "new"}', encoding="utf-8")

    moved = state_layout.migrate(cfg)

    assert moved == ["model_overrides.json"]
    assert (state / "model_overrides.json").read_text(encoding="utf-8") == '{"smart": "openai/x"}'
    assert not (legacy / "model_overrides.json").exists()
    # an existing state file always wins — the legacy copy is left alone
    assert (state / "reviewed_shas.json").read_text(encoding="utf-8") == '{"k": "new"}'
    assert (legacy / "reviewed_shas.json").exists()
    assert not (legacy / ("c" * 64)).exists()  # old-root cache entry dropped
    assert (legacy / "notes.txt").exists()
    assert state_layout.migrate(cfg) == []  # idempotent


def test_prompt_cache_broken_rule():
    from reviewer.ai_client import prompt_cache_broken
    assert prompt_cache_broken(3, 677_000, 0, 0, 100_000)  # !493 tool review
    assert not prompt_cache_broken(1, 677_000, 0, 0, 100_000)  # turn 1 only creates
    assert not prompt_cache_broken(3, 50_000, 0, 0, 100_000)  # small prompt
    assert not prompt_cache_broken(3, 20_000, 600_000, 60_000, 100_000)  # cache works
    assert not prompt_cache_broken(3, 677_000, 0, 0, 0)  # disabled


def test_agent_loop_alerts_once_per_review_on_zero_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from reviewer import usage
    from reviewer.ai_client import ToolDef

    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path)
    cfg.llm.rate_limit = 0
    cfg.llm.cache_alert_min_input = 1000
    monkeypatch.setattr(ai_mod.overrides, "model_for_tier",
                        lambda tier, c: c.model_for_tier(tier))
    alerts: list[tuple[str, str]] = []

    async def alert(kind, details):
        alerts.append((kind, details))

    client = AIClient(cfg, alert=alert)

    def make_stub():
        turns = iter(["tool_use", "end_turn"])

        async def fake_create(**kwargs):
            if next(turns) == "tool_use":
                return SimpleNamespace(
                    stop_reason="tool_use", model="m",
                    content=[SimpleNamespace(type="tool_use", id="t1", name="t", input={})],
                    usage=SimpleNamespace(input_tokens=900, output_tokens=5))
            return SimpleNamespace(
                stop_reason="end_turn", model="m",
                content=[SimpleNamespace(type="text", text="done")],
                usage=SimpleNamespace(input_tokens=900, output_tokens=5))

        stub = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
        stub.with_options = lambda **kw: stub
        return stub

    async def handler(**kw):
        return "out"

    async def review():
        token = usage.current_tracker.set(usage.UsageTracker())
        try:
            for _ in range(2):  # tool review + investigator in one review
                client._primary = make_stub()
                await client.agent_loop("main", "sys", "diff",
                                        [ToolDef("t", "d", {}, handler)], max_iterations=5)
        finally:
            usage.current_tracker.reset(token)

    asyncio.run(review())
    assert len(alerts) == 1
    assert alerts[0][0] == "prompt_cache"
    assert "cached=0" in alerts[0][1]


# --- error text must not leak to MR comments / HTTP responses (review #4) ---

_LEAKY = "connect to http://10.0.0.5:8080/internal failed, see /srv/app/secrets.py"


def test_review_queue_assigns_job_id():
    async def run():
        queue = ReviewQueue(workers=0, dedupe_ttl=600, burst_window=0)
        job = review_job(mr_iid=7, last_commit="abc")
        assert queue.submit(job) is True
        queued = queue.queue.get_nowait()
        assert len(queued.job_id) == 8
        assert queue.submit(replace(job, last_commit="def")) is True
        assert queue.queue.get_nowait().job_id != queued.job_id
    asyncio.run(run())


@pytest.mark.parametrize("exc_factory", [
    lambda: RuntimeError(_LEAKY),
    lambda: ai_mod.AIError(_LEAKY),
])
def test_pipeline_error_note_hides_exception_text(monkeypatch, exc_factory):
    from reviewer import pipeline as pipeline_mod

    notes, alerts = [], []

    async def fake_inner(self, job, ctx):
        raise exc_factory()

    async def fake_note(self, ref, body):
        notes.append(body)

    async def fake_alert(kind, details, ctx=None):
        alerts.append((details, ctx))

    monkeypatch.setattr(pipeline_mod.Pipeline, "_process_inner", fake_inner)
    monkeypatch.setattr(pipeline_mod.Pipeline, "_safe_note", fake_note)
    monkeypatch.setattr(pipeline_mod.telegram_io, "notify_error", fake_alert)

    asyncio.run(pipeline_mod.Pipeline(client=object()).process(
        review_job(job_id="deadbeef")))
    assert len(notes) == 1
    assert "10.0.0.5" not in notes[0] and "/srv/app" not in notes[0]
    assert "deadbeef" in notes[0]
    # details stay available internally
    assert _LEAKY in alerts[0][0]
    assert alerts[0][1]["job_id"] == "deadbeef"


def test_deliver_review_failure_note_hides_exception_text(monkeypatch):
    from types import SimpleNamespace

    from reviewer import pipeline as pipeline_mod

    posted = []

    async def fake_post(mr, body):
        if not posted:
            posted.append(None)
            raise RuntimeError(_LEAKY)
        posted.append(body)

    async def fake_alert(*a, **k):
        return True

    monkeypatch.setattr(pipeline_mod.gitlab_io, "post_note", fake_post)
    monkeypatch.setattr(pipeline_mod.telegram_io, "notify_error", fake_alert)
    ok = asyncio.run(pipeline_mod.Pipeline(client=object())._deliver_review(
        SimpleNamespace(), review_job(job_id="cafe0001"), SimpleNamespace(),
        False, "review"))
    assert ok is False
    assert "10.0.0.5" not in posted[1] and "cafe0001" in posted[1]


def test_webhook_500_hides_exception_text(monkeypatch):
    from fastapi.testclient import TestClient

    from reviewer import server

    def boom(payload, instance):
        raise ValueError(_LEAKY)

    async def fake_alert(*a, **k):
        return True

    monkeypatch.setattr(server.settings.gitlab, "routes", {"hook": INSTANCE})
    monkeypatch.setattr(server.gitlab_io, "parse_merge_request_webhook", boom)
    monkeypatch.setattr(server.telegram_io, "notify_error", fake_alert)
    resp = TestClient(server.app).post(
        "/webhook", json={"object_kind": "merge_request"},
        headers={"X-Gitlab-Token": "hook", "X-Gitlab-Event": "Merge Request Hook"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"] == "internal error"
    assert len(body["job_id"]) == 8
    assert "10.0.0.5" not in resp.text and "/srv/app" not in resp.text


# --- stage 6: legacy gemini / v1-parity paths removed ---

def test_retired_gemini_aliases_are_no_longer_read(monkeypatch):
    for name in ("AI_TIMEOUT", "AI_DEBUG", "AI_CACHE_DIR", "REVIEW_PROMPT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_TIMEOUT", "5")
    monkeypatch.setenv("GEMINI_DEBUG", "true")
    monkeypatch.setenv("GEMINI_CACHE_DIR", "/tmp/old-cache")
    monkeypatch.setenv("GEMINI_PROMPT", "old checklist")
    cfg = Settings()
    assert cfg.llm.timeout == 300
    assert cfg.llm.debug is False
    assert cfg.storage.ai_cache_dir == "cache/ai"
    assert cfg.pipeline.review_prompt == ""
    assert not hasattr(cfg, "pipeline_v2")


def test_review_prompt_override_has_a_non_legacy_name(monkeypatch):
    monkeypatch.setenv("REVIEW_PROMPT", "custom checklist")
    assert Settings().pipeline.review_prompt == "custom checklist"


def test_retired_env_vars_are_reported_with_successor(monkeypatch):
    from reviewer.config import RETIRED_ENV_VARS, retired_env_vars_in_use
    for name in RETIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert retired_env_vars_in_use() == []
    monkeypatch.setenv("PIPELINE_V2", "on")
    monkeypatch.setenv("GEMINI_PROMPT", "x")
    assert retired_env_vars_in_use() == [
        "PIPELINE_V2 is no longer read",
        "GEMINI_PROMPT is no longer read — rename it to REVIEW_PROMPT"]


def test_startup_warns_about_retired_and_deprecated_vars(monkeypatch, caplog, tmp_path):
    from fastapi.testclient import TestClient

    from reviewer import server
    from reviewer.config import settings
    # lifespan runs state_layout.migrate: keep it off the developer's cache/
    monkeypatch.setattr(settings.storage, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings.storage, "ai_cache_dir", str(tmp_path / "cache" / "ai"))
    monkeypatch.setattr(settings.gitlab, "routes", {})
    monkeypatch.setattr(settings.bridge, "enabled", False)
    monkeypatch.setenv("PIPELINE_V2", "off")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_3", "-100x")
    monkeypatch.setattr(server, "review_queue", server.ReviewQueue(
        workers=0, dedupe_ttl=600))
    with caplog.at_level("WARNING", logger="reviewer.server"), TestClient(server.app) as client:
        flags = client.get("/").json()["flags"]
    assert "pipeline_v2" not in flags
    assert "PIPELINE_V2 is no longer read" in caplog.text
    assert "TELEGRAM_CHAT_ID_3" in caplog.text and "deprecated" in caplog.text


def test_single_entry_point_runs_uvicorn_on_port_5000(monkeypatch):
    import uvicorn

    from reviewer import __main__ as entry
    from reviewer.server import app
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    entry.main()
    assert calls == [((app,), {"host": "0.0.0.0", "port": 5000})]


def test_json_store_corrupt_file_reads_as_empty(tmp_path, caplog):
    # stage 7 (#11): a half-written / hand-edited state file must not break
    # reviews — it reads as empty (full review, no overrides), with a warning
    from reviewer.json_store import JsonStore
    path = tmp_path / "state.json"
    path.write_text('{"a": "x", "b"', encoding="utf-8")  # truncated mid-write
    store = JsonStore(path, parse=dict, empty=dict, label="test state")
    with caplog.at_level("WARNING"):
        assert store.read() == {}
    assert "corrupt" in caplog.text
    # valid JSON of the wrong shape is "corrupt" too, not a crash
    path.write_text("[1, 2]", encoding="utf-8")
    store.invalidate()
    assert store.read() == {}
    # a later write replaces the bad file
    store.write({"a": "y"})
    store.invalidate()
    assert store.read() == {"a": "y"}


def test_json_store_failed_write_keeps_old_file(tmp_path, monkeypatch):
    # stage 7 (#11): write_text truncated the file first, so a crash/full disk
    # mid-write left it broken; now the old file survives and no temp is left
    import json as _json

    from reviewer import json_store
    path = tmp_path / "state.json"
    store = json_store.JsonStore(path, parse=dict, empty=dict)
    store.write({"k": "old"})

    def boom(*args, **kwargs):
        raise OSError("No space left on device")
    monkeypatch.setattr(json_store.os, "fsync", boom)
    assert store.write({"k": "new"}) == {"k": "new"}  # fail-open: kept in memory
    assert store.read() == {"k": "new"}
    assert _json.loads(path.read_text(encoding="utf-8")) == {"k": "old"}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # temp cleaned up


def test_json_store_follows_path_change(tmp_path):
    # path is resolved on use: switching STATE_DIR (tests, future reload) reads
    # the new file instead of serving the old dir's in-memory copy
    from reviewer.json_store import JsonStore
    where = {"dir": tmp_path / "a"}
    store = JsonStore(lambda: where["dir"] / "s.json", parse=dict, empty=dict)
    store.write({"x": "1"})
    where["dir"] = tmp_path / "b"
    assert store.read() == {}
    where["dir"] = tmp_path / "a"
    assert store.read() == {"x": "1"}


def test_review_state_survives_corrupt_file(tmp_path, monkeypatch):
    from reviewer import review_state
    from reviewer.config import settings
    monkeypatch.setattr(settings.storage, "state_dir", str(tmp_path))
    (tmp_path / "reviewed_shas.json").write_text("{not json", encoding="utf-8")
    assert review_state.get_last_sha("primary", 1, 2) is None  # -> full review
    review_state.set_last_sha("primary", 1, 2, "abc")
    review_state._store.invalidate()
    assert review_state.get_last_sha("primary", 1, 2) == "abc"


def test_ai_cache_write_is_atomic_and_sweeps_orphan_temps(tmp_path):
    import os
    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path)
    client = AIClient(cfg)
    key = "c" * 64
    client._cache_put(key, "answer")
    assert client._cache_get(key) == "answer"
    assert sorted(p.name for p in tmp_path.iterdir()) == [key]
    # a temp left by a crash mid-write is swept like an expired entry
    orphan = tmp_path / f"{'d' * 64}.abc_123.tmp"
    orphan.write_text("partial", encoding="utf-8")
    old = time.time() - 72 * 3600
    os.utime(orphan, (old, old))
    client._cleanup_cache()
    assert not orphan.exists()
    assert (tmp_path / key).exists()


# --- stage 8: typed config, fail-fast validation, legacy aliases, config.yaml ---

@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No config env vars at all (the developer's .env is already in os.environ)."""
    from reviewer import config
    legacy = [f"{p}{s}" for s in config.LEGACY_INSTANCE_SLOTS
              for p in ("GITLAB_URL", "GITLAB_TOKEN", "XGITLABTOKEN")]
    legacy += [f"TELEGRAM_CHAT_ID{s}" for s in config.LEGACY_CHAT_SLOTS]
    for name in [*config.ENV_FIELDS, *legacy]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "config.yaml"))
    return tmp_path / "config.yaml"


def test_config_bad_values_stop_startup_naming_the_env_var(clean_env, monkeypatch):
    # #9: v1 silently replaced AI_TIMEOUT=abc with 300 and INVESTIGATOR=maybe with off
    from reviewer.config import load_settings
    monkeypatch.setenv("AI_TIMEOUT", "abc")
    monkeypatch.setenv("INVESTIGATOR", "maybe")
    monkeypatch.setenv("AI_PROVIDER", "gemini")  # retired in stage 6 -> error, not "runs as anthropic"
    with pytest.raises(SystemExit) as exc:
        load_settings()
    text = str(exc.value)
    assert "llm.timeout (env AI_TIMEOUT)" in text and "'abc'" in text
    assert "env INVESTIGATOR" in text and "'maybe'" in text
    assert "env AI_PROVIDER" in text


def test_config_instance_without_token_is_an_error(clean_env, monkeypatch):
    # v1 skipped a half-configured instance silently: its webhooks then hit
    # "unknown token" with nothing in the startup log saying why
    from reviewer.config import load_settings
    monkeypatch.setenv("GITLAB_URL_2", "https://b")
    monkeypatch.setenv("XGITLABTOKEN_2", "hook2")
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert "gitlab.legacy_instances.0.token" in str(exc.value)


def test_config_empty_env_values_mean_default(clean_env, monkeypatch):
    # v1 compose passed ${VAR:-} through as "" — that must keep meaning "default"
    monkeypatch.setenv("AI_WORKERS", "")
    monkeypatch.setenv("ANTHROPIC_SMART_MODEL", "")
    monkeypatch.setenv("GITLAB_URL_2", "")
    cfg = Settings()
    assert cfg.server.workers == 2
    assert cfg.llm.tiers.smart.model == "claude-opus-5"
    assert cfg.gitlab.instances == []


def test_config_legacy_telegram_chats_and_new_csv(clean_env, monkeypatch):
    from reviewer.config import deprecated_env_vars_in_use
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_2", "-2")
    cfg = Settings()
    assert cfg.notify.telegram.chat_ids == ["-1", "-2"]
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "-7, -8")
    cfg = Settings()
    assert cfg.notify.telegram.chat_ids == ["-7", "-8"]
    assert "IGNORED" in deprecated_env_vars_in_use(cfg)[0]


YAML_CONFIG = """
gitlab:
  instances:
    - name: primary
      url: https://lab.example
      token: ${LAB_TOKEN}
      webhook_token: ${LAB_HOOK:-fallback-hook}
llm:
  tiers:
    smart: {model: openai/gpt-5.6-terra}
  prices:
    z-ai/glm-5: [0.6, 2.2]
notify:
  telegram:
    chat_ids: ["-100a"]
"""


def test_config_yaml_instances_tiers_prices(clean_env, monkeypatch):
    from reviewer import usage
    from reviewer.config import deprecated_env_vars_in_use
    clean_env.write_text(YAML_CONFIG, encoding="utf-8")
    monkeypatch.setenv("LAB_TOKEN", "glpat-secret")
    monkeypatch.setenv("GITLAB_URL", "https://legacy")  # yaml wins, legacy is reported
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setenv("XGITLABTOKEN", "h")
    monkeypatch.setenv("ANTHROPIC_MAIN_MODEL", "claude-sonnet-5-5")  # env beats yaml/defaults
    cfg = Settings()
    assert cfg.gitlab.routes == {"fallback-hook": InstanceRef(
        "primary", "https://lab.example", "glpat-secret")}
    assert "IGNORED" in deprecated_env_vars_in_use(cfg)[0]
    assert cfg.model_for_tier("smart") == "openai/gpt-5.6-terra"
    assert cfg.fallback_chain("smart")[0] == "anthropic/claude-opus-5"  # default chain kept
    assert cfg.model_for_tier("main") == "claude-sonnet-5-5"
    assert cfg.llm.prices == {"z-ai/glm-5": (0.6, 2.2)}
    assert cfg.notify.telegram.chat_ids == ["-100a"]
    monkeypatch.setattr(usage.settings.llm, "prices", cfg.llm.prices)
    assert usage._load_prices()["z-ai/glm-5"] == (0.6, 2.2)


def test_config_yaml_unknown_tier_or_key_is_an_error(clean_env):
    from reviewer.config import load_settings
    clean_env.write_text("llm:\n  tiers:\n    huge: {model: x}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="llm.tiers.huge"):
        load_settings()
    clean_env.write_text("pipeline:\n  stages:\n    investigatr: true\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="investigatr"):
        load_settings()
    clean_env.write_text("llm: [unclosed\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="config.yaml"):
        load_settings()
    clean_env.unlink()
    with pytest.raises(ValueError, match="unknown tier"):
        Settings().model_for_tier("huge")


def test_config_model_prices_env(clean_env, monkeypatch):
    from reviewer.config import load_settings
    monkeypatch.setenv("MODEL_PRICES", "claude-sonnet-5=3/15, x/y=0.5/1")
    assert Settings().llm.prices == {"claude-sonnet-5": (3.0, 15.0), "x/y": (0.5, 1.0)}
    monkeypatch.setenv("MODEL_PRICES", "claude-sonnet-5=3")  # v1 logged and skipped it
    with pytest.raises(SystemExit, match="MODEL_PRICES"):
        load_settings()


def test_config_prod_env_gives_the_v1_configuration(clean_env, monkeypatch):
    # 8.6: the production .env shape (secrets replaced) yields the same values
    # the v1 dataclass produced — expected values written out from the old
    # _bool/_int/_csv rules, not from the new code
    prod = {
        "GITLAB_URL": "https://lab.smysl.pro", "GITLAB_TOKEN": "glpat-1", "XGITLABTOKEN": "hook-1",
        "GITLAB_URL_2": "https://lab.catzwolf.ru", "GITLAB_TOKEN_2": "glpat-2",
        "XGITLABTOKEN_2": "hook-2",
        "AI_PROVIDER": "anthropic", "ANTHROPIC_API_URL": "https://gw/anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_API_KEY_GATEWAY": "cfut_x",
        "ANTHROPIC_FAST_MODEL": "claude-haiku-4-5", "ANTHROPIC_MAIN_MODEL": "claude-sonnet-5",
        "ANTHROPIC_SMART_MODEL": "claude-opus-5", "OPENROUTER_API_TOKEN": "sk-or-x",
        "INVESTIGATOR": "on", "BRIDGE": "on", "TESTER_REPORT": "on",
        "REVIEW_REPO_TOOLS": "on", "MR_DIALOGUE": "on", "REVIEW_BRIDGE_CHAT_ID": "-100bridge",
        "AI_DEBUG": "false", "REPO_CACHE_MAX_GB": "25",
        "TELEGRAM": "on", "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100team",
        "REVIEW_LANGUAGE": "ru", "REVIEW_FOR_CONFLICT": "false", "DEBUG": "false",
    }
    for name, value in prod.items():
        monkeypatch.setenv(name, value)
    cfg = Settings()
    assert cfg.gitlab.routes == {
        "hook-1": InstanceRef("primary", "https://lab.smysl.pro", "glpat-1"),
        "hook-2": InstanceRef("instance_2", "https://lab.catzwolf.ru", "glpat-2"),
    }
    assert (cfg.llm.provider, cfg.llm.anthropic.api_url, cfg.llm.anthropic.api_key,
            cfg.llm.anthropic.gateway_key, cfg.llm.openrouter.token) == (
        "anthropic", "https://gw/anthropic", "sk-ant-x", "cfut_x", "sk-or-x")
    assert [cfg.model_for_tier(t) for t in ("fast", "main", "smart")] == [
        "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"]
    assert cfg.fallback_chain("main") == [
        "anthropic/claude-sonnet-5", "google/gemini-3.6-flash", "deepseek/deepseek-v4-pro"]
    stages = cfg.pipeline.stages
    assert (stages.investigator, cfg.bridge.enabled, stages.tester_report,
            stages.review_repo_tools, stages.dialogue) == (True, True, True, True, True)
    assert (cfg.llm.cache_ttl, cfg.llm.timeout, cfg.llm.agent_timeout, cfg.llm.rate_limit,
            cfg.llm.max_input_tokens, cfg.llm.cache_alert_min_input, cfg.llm.debug) == (
        3600, 300, 600, 2.0, 300_000, 100_000, False)
    assert (cfg.storage.ai_cache_dir, cfg.storage.state_dir, cfg.storage.log_dir) == (
        "cache/ai", "state", "logs")
    assert (cfg.server.workers, cfg.server.debug, cfg.dedupe.ttl, cfg.dedupe.burst_seconds) == (
        2, False, 600, 30)
    assert (cfg.repo_cache.dir, cfg.repo_cache.max_gb, cfg.repo_cache.ephemeral) == (
        "repos", 25.0, False)
    assert (cfg.pipeline.language, cfg.pipeline.review_for_conflict, cfg.pipeline.review_prompt,
            cfg.pipeline.review_max_tool_calls, cfg.pipeline.dialogue_max_replies_per_mr,
            cfg.pipeline.investigator_max_iterations) == ("ru", False, "", 8, 20, 30)
    assert (cfg.notify.telegram.enabled, cfg.notify.telegram.token,
            cfg.notify.telegram.chat_ids, cfg.notify.telegram.tester_report_chat_ids) == (
        True, "123:abc", ["-100team"], [])
    assert (cfg.bridge.chat_id, cfg.bridge.question_timeout, cfg.bridge.answer_grace,
            cfg.bridge.max_questions_per_mr, cfg.bridge.rate_per_hour,
            cfg.bridge.answer_bot_id) == ("-100bridge", 240, 6.0, 10, 25, "")
    assert cfg.network.proxy_url is None and cfg.llm.prices == {}


def test_config_masked_dump_hides_secrets(clean_env, monkeypatch):
    from reviewer.config import masked_dump
    monkeypatch.setenv("GITLAB_URL", "https://a")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-very-secret")
    monkeypatch.setenv("XGITLABTOKEN", "hook-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    dump = str(masked_dump(Settings()))
    assert "very-secret" not in dump and "hook-secret" not in dump
    assert "https://a" in dump and "max_input_tokens" in dump


def test_config_yaml_referencing_gitlab_token_env_is_not_deprecated(clean_env, monkeypatch):
    # config.example.yaml keeps secrets in the familiar GITLAB_TOKEN/XGITLABTOKEN
    # vars; only GITLAB_URL[_N] marks a legacy env-defined instance
    from reviewer.config import deprecated_env_vars_in_use
    clean_env.write_text(
        "gitlab:\n  instances:\n    - {name: primary, url: https://a, "
        "token: '${GITLAB_TOKEN}', webhook_token: '${XGITLABTOKEN}'}\n", encoding="utf-8")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-1")
    monkeypatch.setenv("XGITLABTOKEN", "hook-1")
    cfg = Settings()
    assert cfg.gitlab.routes["hook-1"].token == "glpat-1"
    assert deprecated_env_vars_in_use(cfg) == []
