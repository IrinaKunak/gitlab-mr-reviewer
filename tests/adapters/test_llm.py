"""AI client and request builder: routing, thinking policy per model, caching, agent loop."""

from __future__ import annotations

import asyncio
import time

import pytest

from reviewer import ai_client as ai_mod
from reviewer import llm_requests
from reviewer.ai_client import AIClient, extract_json
from reviewer.config import Settings
from reviewer.logging_setup import job_context


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
    assert llm_requests.uses_openrouter("anthropic", "claude-sonnet-5") is False
    assert llm_requests.uses_openrouter("anthropic", "openai/gpt-5.6-terra") is True


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
    # #22: a different output budget or effort is a different request
    assert client._cache_key("m", "sys", "user", 4096, None) == key
    assert client._cache_key("m", "sys", "user", 16000, None) != key
    assert client._cache_key("m", "sys", "user", 4096, "high") != key
    assert client._cache_get(key) is None
    client._cache_put(key, "result text")
    assert client._cache_get(key) == "result text"
    # expired entries are misses
    old = tmp_path / key
    import os
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert client._cache_get(key) is None


def test_debug_log_failure_is_nonfatal(tmp_path, caplog):
    # regression: unwritable logs/ mount raised Errno 13 inside _debug and killed the review
    import logging

    from reviewer.logging_setup import AI_DEBUG_LOGGER, configure_ai_debug

    debug = logging.getLogger(AI_DEBUG_LOGGER)
    saved = list(debug.handlers)
    debug.handlers.clear()
    try:
        blocker = tmp_path / "blocker"
        blocker.write_text("")  # file where a directory is needed -> mkdir raises OSError
        assert configure_ai_debug(blocker / "logs") is None  # warned, not raised
        assert "AI debug logging disabled" in caplog.text
        cfg = Settings()
        cfg.llm.debug = True
        AIClient(cfg)._debug("request", "payload")  # no handler: dropped, no raise

        assert configure_ai_debug(tmp_path / "logs") is debug  # writable: dumps land
        AIClient(cfg)._debug("request", "payload")
        for handler in debug.handlers:
            handler.flush()
        assert "request | payload" in (tmp_path / "logs" / "ai-debug.log").read_text()
    finally:
        for handler in debug.handlers:
            handler.close()
        debug.handlers[:] = saved


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


# 12.5: the model table reproduces the old per-model branches one for one —
# (tier, model, requested effort) -> thinking/effort params sent on the gateway
_ADAPTIVE = {"type": "adaptive"}


_PARAM_CASES = [
    # haiku: any thinking param or effort is a 400 — nothing on any tier
    ("fast", "claude-haiku-4-5", None, {}),
    ("main", "claude-haiku-4-5-20251001", "high", {}),
    ("smart", "claude-haiku-4-5", "high", {}),
    # fast: thinking param omitted whatever the model
    ("fast", "claude-sonnet-5", "high", {}),
    # main: thinking OFF the model's own way, never an effort
    # (sonnet-5 runs ADAPTIVE when the param is omitted — prod 2026-07-22)
    ("main", "claude-sonnet-5", None, {"thinking": {"type": "disabled"}}),
    ("main", "claude-sonnet-5", "high", {"thinking": {"type": "disabled"}}),
    ("main", "claude-sonnet-4-6", "high", {"thinking": {"type": "disabled"}}),
    ("main", "claude-opus-4-8", "high", {"thinking": {"type": "disabled"}}),
    # opus-5: disabled thinking is a 400 at xhigh/max -> effort never sent
    ("main", "claude-opus-5", "xhigh", {"thinking": {"type": "disabled"}}),
    ("main", "claude-opus-5", "max", {"thinking": {"type": "disabled"}}),
    # sonnet-5-5 400s on "disabled" (prod 2026-09-29, !127); between_tools
    # takes no other field and 400s at xhigh/max
    ("main", "claude-sonnet-5-5", None, {"thinking": {"type": "between_tools"}}),
    ("main", "claude-sonnet-5-5", "max", {"thinking": {"type": "between_tools"}}),
    ("main", "claude-sonnet-5-5-20261001", "high", {"thinking": {"type": "between_tools"}}),
    # no thinking-off mode at all: adaptive at the lowest effort
    ("main", "claude-opus-5-5", None, {"thinking": _ADAPTIVE, "output_config": {"effort": "low"}}),
    ("main", "claude-fable-5-1", "high",
     {"thinking": _ADAPTIVE, "output_config": {"effort": "low"}}),
    ("main", "claude-mythos-1", None, {"thinking": _ADAPTIVE, "output_config": {"effort": "low"}}),
    # an unknown future model gets the explicit disable (the safe default)
    ("main", "claude-sonnet-6", None, {"thinking": {"type": "disabled"}}),
    # smart: adaptive + the caller's effort (the investigator thinks)
    ("smart", "claude-opus-5", "high", {"thinking": _ADAPTIVE, "output_config": {"effort": "high"}}),
    ("smart", "claude-sonnet-5-5", "high",
     {"thinking": _ADAPTIVE, "output_config": {"effort": "high"}}),
    ("smart", "claude-opus-5-5", "max", {"thinking": _ADAPTIVE, "output_config": {"effort": "max"}}),
    ("smart", "claude-opus-5", None, {"thinking": _ADAPTIVE}),
]


@pytest.mark.parametrize(("tier", "model", "effort", "expected"), _PARAM_CASES)
def test_thinking_params_per_model(tier, model, effort, expected):
    builder = llm_requests.RequestBuilder(Settings())
    assert builder.thinking_params(tier, model, effort) == expected
    request = builder.gateway(tier, model, "sys", [{"role": "user", "content": "u"}], 100,
                              effort=effort)
    assert {k: v for k, v in request.items() if k in ("thinking", "output_config")} == expected
    # never an effort on the main tier unless the table allows it with thinking off
    if tier == "main" and builder.cfg.llm.model_spec(model).thinking_off != "none":
        assert "output_config" not in request


def test_model_table_is_config_not_code(tmp_path, monkeypatch):
    # #8: a new model's thinking mode is a config.yaml entry, not a release
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "llm:\n  models:\n    claude-sonnet-6:\n      thinking_off: between_tools\n"
        "      price: [3, 15]\n    claude-sonnet-5:\n      thinking_off: disabled\n"
        "      max_effort_with_thinking_off: high\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(cfg_file))
    cfg = Settings()
    builder = llm_requests.RequestBuilder(cfg)
    assert builder.thinking_params("main", "claude-sonnet-6", None) == {
        "thinking": {"type": "between_tools"}}
    # an effort cap lets main send effort with thinking off, up to the cap only
    assert builder.thinking_params("main", "claude-sonnet-5", "high") == {
        "thinking": {"type": "disabled"}, "output_config": {"effort": "high"}}
    assert "output_config" not in builder.thinking_params("main", "claude-sonnet-5", "max")
    assert cfg.llm.price_table()["claude-sonnet-6"] == (3.0, 15.0)
    assert "claude-opus-5" in cfg.llm.models  # built-in entries stay


def test_openrouter_models_array_capped():
    # prod !779: smart overridden to openai/gpt-5.6-terra + a 3-entry fallback
    # chain -> 4 models -> OpenRouter 400 "'models' array must have 3 items or
    # fewer" -> the whole investigation was lost after the review had run
    from reviewer.llm_requests import MAX_OPENROUTER_MODELS
    from reviewer.llm_requests import routing_chain as _routing_chain

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
    stripped = llm_requests.strip_cache_control(sent[-1])
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
        cfg.llm.openrouter.token = "sk-or-test"  # tests no longer see a developer's .env
        cfg.storage.ai_cache_dir = str(tmp_path)
        cfg.llm.rate_limit = 0
        cfg.llm.tiers.smart.model = "claude-opus-5"
        cfg.llm.tiers.smart.fallback = [head_model, "moonshotai/kimi-k3"]
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
    assert llm_requests.wants_cache_control(False, "claude-sonnet-5-5") is True


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
    from reviewer.adapters.notify.telegram import usage_footer
    assert "→160009" in usage_footer(tracker.summary())

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
    with job_context(usage=usage.UsageTracker()):
        loop_result = asyncio.run(client.agent_loop(
            "smart", "sys", "user", [ToolDef("t", "d", {}, None)],
            max_iterations=2))
        assert loop_result.cache_read_tokens == 50_000
        recorded = usage.current_tracker().calls[-1]
        assert recorded["input_tokens"] == 9 + 50_000 + 2_000
        assert recorded["cached_tokens"] == 52_000


def test_model_overrides_and_routing(tmp_path, monkeypatch):
    # owner feature 2026-07-23: switch tier models at runtime from the dashboard;
    # vendor-prefixed overrides must route via OpenRouter, claude-* via gateway
    import asyncio
    from types import SimpleNamespace

    from reviewer.overrides import ModelOverrides

    cfg = Settings()
    cfg.storage.ai_cache_dir = str(tmp_path)
    cfg.llm.openrouter.token = "sk-or-test"  # tests no longer see a developer's .env
    overrides = ModelOverrides(tmp_path, cfg)
    assert overrides.model_for_tier("smart") == cfg.llm.tiers.smart.model  # env default
    overrides.save({"smart": "openai/gpt-5.6-terra", "junk": "ignored"})
    assert overrides.model_for_tier("smart") == "openai/gpt-5.6-terra"
    assert overrides.load()["fast"] == ""  # untouched tiers stay on defaults
    assert ModelOverrides(tmp_path, cfg).load()["smart"] == "openai/gpt-5.6-terra"  # persisted

    # complete() with the override must call the OpenRouter client, not primary
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn", model="openai/gpt-5.6-terra",
            usage=SimpleNamespace(input_tokens=5, output_tokens=5))

    client = AIClient(cfg, overrides=overrides)
    client._fallback = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    client._primary = None  # would explode if touched — proves routing
    result = asyncio.run(client.complete("smart", "sys", "user", use_cache=False))
    assert result.provider == "openrouter"
    assert captured["model"] == "openai/gpt-5.6-terra"
    assert captured["extra_body"]["models"][0] == "openai/gpt-5.6-terra"


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
    with job_context(usage=tracker):
        result = asyncio.run(client.agent_loop(
            "smart", "sys", "go", tools=[], max_iterations=3))

    assert result.input_tokens == 300 and result.output_tokens == 30
    assert len(tracker.calls) == 1  # a single aggregate record for the loop
    assert tracker.calls[0]["input_tokens"] == 300
    assert tracker.calls[0]["output_tokens"] == 30


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
    cleaned = llm_requests.strip_thinking(messages)
    types = [b["type"] for b in cleaned[1]["content"]]
    assert types == ["text", "tool_use"]
    assert messages[1]["content"][0]["type"] == "thinking"  # original untouched


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
        with job_context(usage=usage.UsageTracker()):
            for _ in range(2):  # tool review + investigator in one review
                client._primary = make_stub()
                await client.agent_loop("main", "sys", "diff",
                                        [ToolDef("t", "d", {}, handler)], max_iterations=5)

    asyncio.run(review())
    assert len(alerts) == 1
    assert alerts[0][0] == "prompt_cache"
    assert "cached=0" in alerts[0][1]


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
