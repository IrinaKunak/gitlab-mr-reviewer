"""Tiered AI client. Replaces gemini-wrapper.sh.

Request bodies, routing and the per-model thinking policy live in
llm_requests.RequestBuilder (driven by the model table in config).

Primary:  Anthropic Messages API via Cloudflare AI Gateway.
          cfut_ gateway tokens go in the `cf-aig-authorization: Bearer ...` header
          (NOT x-api-key); a real Anthropic key is passed as api_key directly.
Fallback: OpenRouter's Anthropic-compatible /api/v1/messages with a `models` array
          (same-class Claude slug first, then cross-vendor) — reuses the same SDK.
          CF Dynamic Routing cannot target OpenRouter, so failover is client-side.
          AI_PROVIDER=openrouter skips the gateway and sends every tier there.

Preserved wrapper behaviors: response cache (sha256 + TTL, success-only), rate
limiting (now an asyncio lock, not a /tmp file), timeout with a distinct error,
input size guard, debug request/response logging.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import anthropic
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from . import usage
from .config import Settings
from .domain.models import Tier
from .json_store import atomic_write_text
from .llm_requests import CACHE_CONTROL, RequestBuilder, Route
from .overrides import ModelOverrides
from .usage import UsageAccumulator

logger = logging.getLogger(__name__)

# appended when a response hits max_tokens; a result that is ONLY this marker
# produced no visible text (adaptive thinking consumed the whole output budget)
TRUNCATION_MARKER = "*[response truncated at the output limit]*"

# Cross-vendor fallbacks may reject Anthropic-specific params — sent only to primary.
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)


def _should_fallback(exc: Exception) -> bool:
    """Fail over only on availability problems (429/5xx/529/network/timeout).

    4xx errors are OUR bug or gateway misconfiguration — falling back would
    mask them and double-bill; surface them instead.
    """
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


class AIError(Exception):
    """All providers failed."""


class AITimeoutError(AIError):
    """The request timed out on every provider."""


class AIInputTooLargeError(AIError):
    """Input exceeds the configured token budget."""


@dataclass
class AIResult:
    text: str
    model: str = ""
    provider: str = ""  # "gateway" | "openrouter" | "cache"
    input_tokens: int = 0
    output_tokens: int = 0
    # prompt-cache tokens are reported SEPARATELY from input_tokens on the wire
    # (auto-caching models via OpenRouter put nearly the whole prompt here)
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cached: bool = False


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable[str]] = field(repr=False, default=None)  # type: ignore[assignment]

    def to_api(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


# measured on prod MR !779: 580k chars of diff billed 290,883 input tokens — 2.0
# chars/token. The old //3 under-counted by ~50%, so budgets admitted far more
# than intended (a "193k" review actually cost 291k tokens).
CHARS_PER_TOKEN = 2


def estimate_tokens(text: str) -> int:
    """Cheap upper-ish estimate; avoids a count_tokens round-trip per request."""
    return len(text) // CHARS_PER_TOKEN


def extract_json(text: str) -> dict | None:
    """Lenient JSON extraction for fallback models without structured outputs."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.DOTALL)
    if not match:
        match = re.search(r"(\{.*\})", text or "", re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


CACHE_KEY_RE = re.compile(r"[0-9a-f]{64}")  # _cache_key output: sha256 hexdigest
# a cache entry, or the temp file atomic_write_text left behind if it died mid-write
CACHE_SWEEP_RE = re.compile(r"[0-9a-f]{64}(\.[^.]+\.tmp)?")


def prompt_cache_broken(iterations: int, input_tokens: int, cache_read: int,
                        cache_creation: int, min_input: int) -> bool:
    """A multi-turn agent loop that read a big prompt with zero cache reads.

    Turn 1 can only create the cache, so single-turn loops never qualify.
    Every later turn re-sends the whole prefix — 0 reads there means each
    iteration was billed at full input price (!493: 677k in, 0 cached).
    """
    total = input_tokens + cache_read + cache_creation
    return min_input > 0 and iterations > 1 and total >= min_input and cache_read == 0


AlertFn = Callable[[str, str], Awaitable[Any]]


class AIClient:
    def __init__(self, cfg: Settings, overrides: ModelOverrides | None = None,
                 alert: AlertFn | None = None):
        self.cfg = cfg
        # dashboard per-tier model overrides; None = the configured models only
        self.overrides = overrides
        self._alert = alert  # (error_type, details) -> ops notification; None = log only
        self._rate_lock = asyncio.Lock()
        self._last_call = 0.0
        self._cache_dir = Path(self.cfg.storage.ai_cache_dir)
        self._debug_logger: logging.Logger | None = None
        self._debug_failed = False
        self._primary: AsyncAnthropic | None = None
        self._fallback: AsyncAnthropic | None = None
        self.requests = RequestBuilder(cfg)

    def _model_for(self, tier: Tier) -> str:
        if self.overrides is not None:
            return self.overrides.model_for_tier(tier)
        return self.cfg.model_for_tier(tier)

    # --- client construction (lazy: keys may be absent in tests) ---

    def _http_client(self, timeout: float) -> DefaultAsyncHttpxClient:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self.cfg.network.proxy_url:
            kwargs["proxy"] = self.cfg.network.proxy_url
        return DefaultAsyncHttpxClient(**kwargs)

    @property
    def primary(self) -> AsyncAnthropic:
        """Cloudflare AI Gateway auth modes (developers.cloudflare.com, verified 2026-06):

        1. real key + cfut_ token  -> x-api-key + cf-aig-authorization
           (key-in-request through an authenticated gateway)
        2. cfut_ token only        -> cf-aig-authorization, dummy x-api-key
           (BYOK / Unified Billing: the gateway injects the provider credential)
        3. real key only           -> plain x-api-key (unauthenticated gateway / direct API)
        """
        if self._primary is None:
            real_key = self.cfg.llm.anthropic.api_key
            gateway_key = self.cfg.llm.anthropic.gateway_key
            if not real_key and gateway_key and not gateway_key.startswith("cfut_"):
                # legacy single-var setup: a real key stored in the GATEWAY var
                real_key, gateway_key = gateway_key, ""
            headers = ({"cf-aig-authorization": f"Bearer {gateway_key}"}
                       if gateway_key.startswith("cfut_") else None)
            self._primary = AsyncAnthropic(
                base_url=self.cfg.llm.anthropic.api_url or None,
                api_key=real_key or ("gateway" if headers else None),
                default_headers=headers,
                http_client=self._http_client(self.cfg.llm.timeout),
                # 1, not 2: a timed-out big request is still billed server-side —
                # retries multiply cost; real outages go to the OpenRouter fallback
                max_retries=1,
            )
        return self._primary

    @property
    def fallback(self) -> AsyncAnthropic | None:
        if not self.cfg.llm.openrouter.token:
            return None
        if self._fallback is None:
            self._fallback = AsyncAnthropic(
                base_url="https://openrouter.ai/api",
                auth_token=self.cfg.llm.openrouter.token,
                http_client=self._http_client(self.cfg.llm.timeout),
                max_retries=1,
            )
        return self._fallback

    # --- prompt-cache monitoring ---

    async def _check_prompt_cache(self, tier: Tier, model: str, provider: str,
                                  iterations: int, total_in: int,
                                  total_cr: int, total_cc: int) -> None:
        """Warn (and alert once per review) when an agent loop paid full price
        for every turn. Fail-open: monitoring must never break a review."""
        if not prompt_cache_broken(iterations, total_in, total_cr, total_cc,
                                   self.cfg.llm.cache_alert_min_input):
            return
        details = (f"{tier} tier, {model} via {provider}: {iterations} turns, "
                   f"in={total_in + total_cc} cached=0 — every turn re-billed the "
                   f"full prompt. Check the tier's model override / AI_PROVIDER.")
        logger.warning("prompt cache miss: %s", details)
        tracker = usage.current_tracker.get()
        if self._alert is None or (tracker is not None and tracker.cache_alerted):
            return
        if tracker is not None:
            tracker.cache_alerted = True
        try:
            await self._alert("prompt_cache", details)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prompt cache alert not sent: %s", exc)

    # --- cache (success-only, sha256 key, TTL) ---

    def _cache_key(self, model: str, system: str, user_content: str,
                   max_tokens: int = 4096, effort: str | None = None) -> str:
        """Everything that changes the answer: a different output budget or
        effort is a different request, never a cache hit (#22)."""
        digest = hashlib.sha256()
        for part in (model, system, user_content, f"max_tokens={max_tokens}",
                     f"effort={effort or ''}"):
            digest.update(part.encode("utf-8", errors="replace"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _cache_get(self, key: str) -> str | None:
        path = self._cache_dir / key
        try:
            if path.is_file() and (time.time() - path.stat().st_mtime) < self.cfg.llm.cache_ttl:
                return path.read_text(encoding="utf-8")
        except OSError:
            pass
        return None

    def _cache_put(self, key: str, text: str) -> None:
        if not text:
            return
        try:
            atomic_write_text(self._cache_dir / key, text)  # no torn entry is ever read
            self._cleanup_cache()
        except OSError as exc:
            logger.warning("cache write failed: %s", exc)

    def _cleanup_cache(self, max_age_hours: int = 48) -> None:
        # only our own sha256-named entries: the sweep once shared a dir with
        # model_overrides.json / reviewed_shas.json and deleted them after 48h
        # idle, silently reverting dashboard overrides on the next restart
        cutoff = time.time() - max_age_hours * 3600
        try:
            for entry in self._cache_dir.iterdir():
                if (entry.is_file() and CACHE_SWEEP_RE.fullmatch(entry.name)
                        and entry.stat().st_mtime < cutoff):
                    entry.unlink(missing_ok=True)
        except OSError:
            pass

    # --- rate limit & debug log ---

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            wait = self.cfg.llm.rate_limit - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    def _debug(self, direction: str, payload: str) -> None:
        if not self.cfg.llm.debug or self._debug_failed:
            return
        try:
            if self._debug_logger is None:
                log_dir = Path(self.cfg.storage.log_dir)
                log_dir.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    log_dir / "ai-debug.log", maxBytes=20_000_000, backupCount=3, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                self._debug_logger = logging.getLogger("reviewer.ai_debug")
                self._debug_logger.addHandler(handler)
                self._debug_logger.setLevel(logging.DEBUG)
                self._debug_logger.propagate = False
            self._debug_logger.debug("%s | %s", direction, payload[:50_000])
        except OSError as exc:
            # debug logging must never take down a review (e.g. unwritable
            # bind-mounted logs/ dir) — warn once and continue without it
            self._debug_failed = True
            logger.warning("AI debug logging disabled (%s) — reviews continue without it", exc)

    # --- public API ---

    def guard_input_size(self, *parts: str) -> None:
        total = sum(estimate_tokens(p) for p in parts)
        if total > self.cfg.llm.max_input_tokens:
            raise AIInputTooLargeError(
                f"input ~{total} tokens exceeds limit {self.cfg.llm.max_input_tokens}")

    async def complete(
        self,
        tier: Tier,
        system: str,
        user_content: str,
        *,
        max_tokens: int = 4096,
        effort: str | None = None,
        json_schema: dict | None = None,
        use_cache: bool = True,
        timeout: float | None = None,
    ) -> AIResult:
        """Single-shot completion with primary -> fallback failover."""
        self.guard_input_size(system, user_content)
        route = self.requests.route(tier, self._model_for(tier))
        model = route.model

        cache_key = self._cache_key(model, system, user_content, max_tokens, effort)
        if use_cache and not json_schema:
            cached = self._cache_get(cache_key)
            if cached is not None:
                logger.info("ai cache hit tier=%s", tier)
                # $0, but visible in per-review call counts
                usage.record(tier=tier, model=model, provider="cache",
                             input_tokens=0, output_tokens=0)
                return AIResult(text=cached, model=model, provider="cache", cached=True)

        await self._rate_limit()
        messages = [{"role": "user", "content": user_content}]
        self._debug("request", f"tier={tier} model={model} system={system[:500]} "
                               f"user={user_content[:2000]}")

        started = time.monotonic()
        if route.via_openrouter:
            # AI_PROVIDER=openrouter, or a vendor-prefixed id the CF gateway
            # cannot serve. The tier chain rides along as OpenRouter failover.
            result = await self._fallback_complete(
                tier, system, messages, max_tokens, json_schema, timeout,
                cause=None, chain=route.chain)
            return self._finish(tier, result, cache_key, use_cache, json_schema, started)

        request = self.requests.gateway(tier, model, system, messages, max_tokens,
                                        effort=effort, json_schema=json_schema)

        try:
            response = await self._call(self.primary, request, timeout)
            result = self._to_result(response, "gateway")
            if result.text == TRUNCATION_MARKER and max_tokens < 64000:
                # adaptive thinking can eat the entire budget before any text
                # (seen with sonnet-5 on a huge diff) — one retry with 4x the room
                logger.warning("no visible text at max_tokens=%d -> retry with %d",
                               max_tokens, max_tokens * 4)
                request["max_tokens"] = max_tokens * 4
                response = await self._call(self.primary, request, timeout)
                result = self._to_result(response, "gateway")
        except _RETRYABLE + (anthropic.APIStatusError,) as exc:
            if not _should_fallback(exc):
                raise self._wrap(exc) from exc
            logger.warning("primary failed (%s) -> openrouter fallback", type(exc).__name__)
            result = await self._fallback_complete(
                tier, system, messages, max_tokens, json_schema, timeout, cause=exc)

        return self._finish(tier, result, cache_key, use_cache, json_schema, started)

    def _finish(self, tier: Tier, result: AIResult, cache_key: str,
                use_cache: bool, json_schema: dict | None, started: float) -> AIResult:
        logger.info(
            "ai ok tier=%s model=%s provider=%s in=%d cached=%d out=%d %.1fs",
            tier, result.model, result.provider, result.input_tokens,
            result.cache_read_tokens + result.cache_creation_tokens,
            result.output_tokens, time.monotonic() - started)
        usage.record(tier=tier, model=result.model, provider=result.provider,
                     input_tokens=result.input_tokens,
                     output_tokens=result.output_tokens,
                     cache_read_tokens=result.cache_read_tokens,
                     cache_creation_tokens=result.cache_creation_tokens)
        self._debug("response", f"provider={result.provider} text={result.text[:5000]}")
        if use_cache and not json_schema and result.text != TRUNCATION_MARKER:
            # never cache a no-text truncation — it would poison retries for an hour
            self._cache_put(cache_key, result.text)
        return result

    async def complete_json(self, tier: Tier, system: str, user_content: str,
                            schema: dict, *, max_tokens: int = 2048) -> dict | None:
        """Structured completion: schema-enforced on primary, lenient parse on fallback."""
        result = await self.complete(tier, system, user_content, max_tokens=max_tokens,
                                     json_schema=schema, use_cache=False)
        return extract_json(result.text)

    async def _fallback_complete(self, tier: Tier, system: str, messages: list,
                                 max_tokens: int, json_schema: dict | None,
                                 timeout: float | None, cause: Exception | None,
                                 chain: list[str] | None = None) -> AIResult:
        client = self.fallback
        if client is None:
            raise (self._wrap(cause) if cause is not None
                   else AIError("OpenRouter routing requires OPENROUTER_API_TOKEN"))
        request = self.requests.openrouter(chain or self.requests.fallback_chain(tier),
                                           system, messages, max_tokens,
                                           json_schema=json_schema)
        try:
            response = await self._call(client, request, timeout)
            return self._to_result(response, "openrouter")
        except Exception as exc:  # noqa: BLE001 — both providers down: surface as AIError
            raise self._wrap(exc) from cause

    async def _call(self, client: AsyncAnthropic, request: dict, timeout: float | None):
        opts = client.with_options(timeout=timeout) if timeout else client
        return await opts.messages.create(**request)

    def _to_result(self, response: Any, provider: str) -> AIResult:
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise AIError("model refused the request (stop_reason=refusal)")
        text = "".join(block.text for block in response.content
                       if getattr(block, "type", "") == "text")
        if stop_reason == "max_tokens":
            logger.warning("response truncated at max_tokens (provider=%s)", provider)
            text += "\n\n" + TRUNCATION_MARKER
        usage_info = getattr(response, "usage", None)
        return AIResult(
            text=text.strip(),
            model=getattr(response, "model", ""),
            provider=provider,
            input_tokens=getattr(usage_info, "input_tokens", 0) or 0,
            output_tokens=getattr(usage_info, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage_info, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(
                usage_info, "cache_creation_input_tokens", 0) or 0,
        )

    def _wrap(self, exc: Exception) -> AIError:
        if isinstance(exc, anthropic.APITimeoutError):
            return AITimeoutError(str(exc))
        return AIError(f"{type(exc).__name__}: {exc}")

    # --- agentic loop (investigator, tool-assisted review, dialogue) ---

    async def agent_loop(
        self,
        tier: Tier,
        system: str,
        user_content: str,
        tools: list[ToolDef],
        *,
        max_iterations: int = 30,
        max_tokens: int = 16000,
        effort: str = "high",
    ) -> AIResult:
        """Manual tool loop on the primary provider (fallback on per-call failures).

        Thinking blocks are passed back verbatim (required for adaptive thinking
        with tool use). Tool handlers are async callables returning strings.
        """
        self.guard_input_size(system, user_content)
        route = self.requests.route(tier, self._model_for(tier))
        loop = _AgentLoop(tier=tier, route=route, system=system, max_tokens=max_tokens,
                          effort=effort, tools=[tool.to_api() for tool in tools],
                          handlers={tool.name: tool.handler for tool in tools})
        # cache the static prefix (tools + system + the MR diff/context render
        # ahead of it): every iteration re-sends it, so without this the whole
        # investigation is billed at full input price on each turn
        first_turn: dict = {"type": "text", "text": user_content}
        if route.cache_control:
            first_turn["cache_control"] = dict(CACHE_CONTROL)
        loop.messages.append({"role": "user", "content": [first_turn]})

        try:
            for iteration in range(max_iterations):
                await self._rate_limit()
                response = await self._send_turn(loop)
                loop.usage.add(getattr(response, "usage", None))
                loop.last_text = "".join(block.text for block in response.content
                                         if getattr(block, "type", "") == "text") or loop.last_text

                if response.stop_reason == "pause_turn":
                    # Resume a paused turn: keep the FULL history and echo the paused
                    # assistant content verbatim (thinking blocks included) — truncating
                    # to messages[:1] would drop all prior tool_use/tool_result context.
                    loop.messages.append({"role": "assistant", "content": response.content})
                    continue
                if response.stop_reason == "refusal":
                    raise AIError("model refused during investigation (stop_reason=refusal)")
                if response.stop_reason != "tool_use":
                    logger.info("agent_loop done after %d iterations stop=%s in=%d out=%d",
                                iteration + 1, response.stop_reason,
                                loop.usage.input_tokens, loop.usage.output_tokens)
                    return await self._finish_loop(loop, iteration + 1)

                loop.messages.append({"role": "assistant", "content": response.content})
                results = await self._run_tools(response, loop.handlers)
                self._advance_cache_breakpoint(loop, results)
                loop.messages.append({"role": "user", "content": results})

            logger.warning("agent_loop hit max_iterations=%d", max_iterations)
            return await self._finish_loop(loop, max_iterations)
        finally:
            # every exit path, errors included: completed turns are real spend
            loop.usage.record(tier=tier, model=route.model, provider=loop.provider)

    async def _send_turn(self, loop: _AgentLoop) -> Any:
        """One Messages call on the loop's route; a gateway outage fails over to
        the tier's OpenRouter chain for this turn. Sets `loop.provider`."""
        route, timeout = loop.route, self.cfg.llm.agent_timeout
        try:
            if route.via_openrouter:
                client = self.fallback
                if client is None:
                    raise AIError("OpenRouter routing requires OPENROUTER_API_TOKEN")
                request = self.requests.openrouter(route.chain, loop.system, loop.messages,
                                                   loop.max_tokens, tools=loop.tools)
                response = await self._call(client, request, timeout)
                loop.provider = "openrouter"
            else:
                request = self.requests.gateway(loop.tier, route.model, loop.system,
                                                loop.messages, loop.max_tokens,
                                                effort=loop.effort, tools=loop.tools)
                response = await self._call(self.primary, request, timeout)
                loop.provider = "gateway"
            return response
        except _RETRYABLE + (anthropic.APIStatusError,) as exc:
            if not _should_fallback(exc):
                raise self._wrap(exc) from exc
            logger.warning("agent_loop primary failed (%s), trying openrouter",
                           type(exc).__name__)
            fallback = self.fallback
            if fallback is None:
                raise self._wrap(exc) from exc
            request = self.requests.openrouter(self.requests.fallback_chain(loop.tier),
                                               loop.system, loop.messages, loop.max_tokens,
                                               tools=loop.tools)
            try:
                response = await self._call(fallback, request, timeout)
            except Exception as exc2:  # noqa: BLE001
                raise self._wrap(exc2) from exc
            loop.provider = "openrouter"
            return response

    async def _run_tools(self, response: Any,
                         handlers: dict[str, Callable[..., Awaitable[str]]]) -> list[dict]:
        """Execute every tool_use block; errors go back to the model as results."""
        results = []
        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            handler = handlers.get(block.name)
            self._debug("tool_use", f"{block.name} {json.dumps(block.input)[:2000]}")
            if handler is None:
                output, is_error = f"Unknown tool: {block.name}", True
            else:
                try:
                    output, is_error = await handler(**(block.input or {})), False
                except Exception as exc:  # noqa: BLE001 — feed errors back to the model
                    output, is_error = f"Tool error: {exc}", True
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": str(output)[:60_000], "is_error": is_error})
        return results

    @staticmethod
    def _advance_cache_breakpoint(loop: _AgentLoop, results: list[dict]) -> None:
        """Roll the second breakpoint forward so each turn also reads the growing
        history; drop the previous one (≤4 breakpoints per request)."""
        if not results or not loop.route.cache_control:
            return
        if loop.rolling_cache is not None:
            loop.rolling_cache.pop("cache_control", None)
        results[-1]["cache_control"] = dict(CACHE_CONTROL)
        loop.rolling_cache = results[-1]

    async def _finish_loop(self, loop: _AgentLoop, iterations: int) -> AIResult:
        acc = loop.usage
        await self._check_prompt_cache(loop.tier, loop.route.model, loop.provider, iterations,
                                       acc.input_tokens, acc.cache_read_tokens,
                                       acc.cache_creation_tokens)
        return AIResult(text=loop.last_text.strip(), model=loop.route.model,
                        provider=loop.provider, input_tokens=acc.input_tokens,
                        output_tokens=acc.output_tokens,
                        cache_read_tokens=acc.cache_read_tokens,
                        cache_creation_tokens=acc.cache_creation_tokens)


@dataclass
class _AgentLoop:
    """State of one agent_loop run."""
    tier: Tier
    route: Route
    system: str
    max_tokens: int
    effort: str | None
    tools: list[dict]
    handlers: dict[str, Callable[..., Awaitable[str]]]
    messages: list[dict] = field(default_factory=list)
    usage: UsageAccumulator = field(default_factory=UsageAccumulator)
    rolling_cache: dict | None = None  # the moving 2nd cache breakpoint
    provider: str = "gateway"
    last_text: str = ""
