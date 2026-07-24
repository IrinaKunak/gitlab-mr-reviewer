"""Tiered AI client. Replaces gemini-wrapper.sh.

Primary:  Anthropic Messages API via Cloudflare AI Gateway.
          cfut_ gateway tokens go in the `cf-aig-authorization: Bearer ...` header
          (NOT x-api-key); a real Anthropic key is passed as api_key directly.
Fallback: OpenRouter's Anthropic-compatible /api/v1/messages with a `models` array
          (same-class Claude slug first, then cross-vendor) — reuses the same SDK.
          CF Dynamic Routing cannot target OpenRouter, so failover is client-side.

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
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable

import anthropic
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from . import overrides, usage
from .config import Settings, settings as default_settings

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


# Anthropic prompt caching is OPT-IN: without a cache_control breakpoint every
# agent-loop iteration re-bills the whole repo/diff prefix at full price. (The
# auto-caching models we reach through OpenRouter do this for us — that asymmetry
# is why terra investigations cost ~half what the same loop costs on the gateway.)
CACHE_CONTROL = {"type": "ephemeral"}  # 5-min TTL: reads 0.1x, writes 1.25x


def _strip_cache_control(messages: list[dict]) -> list[dict]:
    """Remove cache_control markers before sending to OpenRouter — cross-vendor
    models auto-cache and may reject Anthropic-specific block fields."""
    cleaned: list[dict] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = [{k: v for k, v in block.items() if k != "cache_control"}
                       if isinstance(block, dict) else block
                       for block in content]
            cleaned.append({**message, "content": content})
        else:
            cleaned.append(message)
    return cleaned


def _strip_thinking(messages: list[dict]) -> list[dict]:
    """Drop thinking blocks from assistant turns for cross-vendor fallback requests
    (non-Claude models can reject replayed thinking blocks with no thinking param)."""
    cleaned: list[dict] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            content = [block for block in content
                       if getattr(block, "type", None) not in ("thinking", "redacted_thinking")
                       and (not isinstance(block, dict)
                            or block.get("type") not in ("thinking", "redacted_thinking"))]
            cleaned.append({**message, "content": content})
        else:
            cleaned.append(message)
    return cleaned


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


def estimate_tokens(text: str) -> int:
    """Cheap upper-ish estimate; avoids a count_tokens round-trip per request."""
    return len(text) // 3


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


class AIClient:
    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings
        self._rate_lock = asyncio.Lock()
        self._last_call = 0.0
        self._cache_dir = Path(self.cfg.ai_cache_dir)
        self._debug_logger: logging.Logger | None = None
        self._debug_failed = False
        self._primary: AsyncAnthropic | None = None
        self._fallback: AsyncAnthropic | None = None

    # --- client construction (lazy: keys may be absent in tests) ---

    def _http_client(self, timeout: float) -> DefaultAsyncHttpxClient:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self.cfg.proxy_url:
            kwargs["proxy"] = self.cfg.proxy_url
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
            real_key = self.cfg.anthropic_api_key
            gateway_key = self.cfg.anthropic_gateway_key
            if not real_key and gateway_key and not gateway_key.startswith("cfut_"):
                # legacy single-var setup: a real key stored in the GATEWAY var
                real_key, gateway_key = gateway_key, ""
            headers = ({"cf-aig-authorization": f"Bearer {gateway_key}"}
                       if gateway_key.startswith("cfut_") else None)
            self._primary = AsyncAnthropic(
                base_url=self.cfg.anthropic_api_url or None,
                api_key=real_key or ("gateway" if headers else None),
                default_headers=headers,
                http_client=self._http_client(self.cfg.ai_timeout),
                # 1, not 2: a timed-out big request is still billed server-side —
                # retries multiply cost; real outages go to the OpenRouter fallback
                max_retries=1,
            )
        return self._primary

    @property
    def fallback(self) -> AsyncAnthropic | None:
        if not self.cfg.openrouter_token:
            return None
        if self._fallback is None:
            self._fallback = AsyncAnthropic(
                base_url="https://openrouter.ai/api",
                auth_token=self.cfg.openrouter_token,
                http_client=self._http_client(self.cfg.ai_timeout),
                max_retries=1,
            )
        return self._fallback

    # --- model params per tier ---

    @staticmethod
    def _record_agent_usage(tier: str, model: str, provider: str,
                            total_in: int, total_out: int,
                            total_cr: int, total_cc: int) -> None:
        """Record accumulated agent-loop tokens — called on EVERY exit path;
        a failed investigation's completed turns are still real spend."""
        if total_in or total_out or total_cr or total_cc:
            usage.record(tier=tier, model=model, provider=provider,
                         input_tokens=total_in, output_tokens=total_out,
                         cache_read_tokens=total_cr, cache_creation_tokens=total_cc)

    def _primary_params(self, tier: str, effort: str | None, model: str = "") -> dict:
        """Thinking/effort config valid for the primary Claude model of this tier."""
        params: dict[str, Any] = {}
        if model.startswith("claude-haiku"):
            # haiku (any tier, e.g. via runtime override): no thinking param,
            # no effort — both 400 on it
            return params
        if tier == "smart":
            # only the investigator thinks: on big-diff reviews adaptive thinking
            # ate the entire max_tokens budget before emitting any text (prod,
            # 2026-07-22) while the non-thinking fallback wrote a great review
            params["thinking"] = {"type": "adaptive"}
            if effort:
                params["output_config"] = {"effort": effort}
        # NB fast/main fall through to thinking=disabled with NO effort. On
        # claude-opus-5 disabled thinking is a 400 at effort xhigh/max but fine
        # at the default (high) — so effort must stay unset on those tiers.
        elif tier == "main":
            # sonnet-5 runs ADAPTIVE thinking when the param is omitted (changed
            # from sonnet-4-6!) — disabling must be explicit for predictable cost
            params["thinking"] = {"type": "disabled"}
        # fast = haiku-4-5: omitted param = no thinking; no effort (400s on Haiku)
        return params

    # --- cache (success-only, sha256 key, TTL) ---

    def _cache_key(self, model: str, system: str, user_content: str) -> str:
        digest = hashlib.sha256()
        for part in (model, system, user_content):
            digest.update(part.encode("utf-8", errors="replace"))
        return digest.hexdigest()

    def _cache_get(self, key: str) -> str | None:
        path = self._cache_dir / key
        try:
            if path.is_file() and (time.time() - path.stat().st_mtime) < self.cfg.ai_cache_ttl:
                return path.read_text(encoding="utf-8")
        except OSError:
            pass
        return None

    def _cache_put(self, key: str, text: str) -> None:
        if not text:
            return
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            (self._cache_dir / key).write_text(text, encoding="utf-8")
            self._cleanup_cache()
        except OSError as exc:
            logger.warning("cache write failed: %s", exc)

    def _cleanup_cache(self, max_age_hours: int = 48) -> None:
        cutoff = time.time() - max_age_hours * 3600
        try:
            for entry in self._cache_dir.iterdir():
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
        except OSError:
            pass

    # --- rate limit & debug log ---

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            wait = self.cfg.ai_rate_limit - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    def _debug(self, direction: str, payload: str) -> None:
        if not self.cfg.ai_debug or self._debug_failed:
            return
        try:
            if self._debug_logger is None:
                log_dir = Path(self.cfg.ai_log_dir)
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
        if total > self.cfg.ai_max_input_tokens:
            raise AIInputTooLargeError(
                f"input ~{total} tokens exceeds limit {self.cfg.ai_max_input_tokens}")

    async def complete(
        self,
        tier: str,
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
        model = overrides.model_for_tier(tier, self.cfg)

        cache_key = self._cache_key(model, system, user_content)
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
        if "/" in model:
            # vendor-prefixed runtime override (e.g. openai/gpt-5.6-terra):
            # the CF gateway can't serve it — route via OpenRouter, with the
            # tier's regular chain behind it as backup
            chain = [model] + [m for m in self.cfg.fallback_chain(tier) if m != model]
            result = await self._fallback_complete(
                tier, system, messages, max_tokens, json_schema, timeout,
                cause=None, chain=chain)
            return self._finish(tier, result, cache_key, use_cache, json_schema, started)

        request: dict[str, Any] = {
            "model": model, "system": system, "messages": messages,
            "max_tokens": max_tokens, **self._primary_params(tier, effort, model),
        }
        if json_schema:
            request["output_config"] = {
                **request.get("output_config", {}),
                "format": {"type": "json_schema", "schema": json_schema},
            }

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
                raise self._wrap(exc)
            logger.warning("primary failed (%s) -> openrouter fallback", type(exc).__name__)
            result = await self._fallback_complete(
                tier, system, messages, max_tokens, json_schema, timeout, cause=exc)

        return self._finish(tier, result, cache_key, use_cache, json_schema, started)

    def _finish(self, tier: str, result: AIResult, cache_key: str,
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

    async def complete_json(self, tier: str, system: str, user_content: str,
                            schema: dict, *, max_tokens: int = 2048) -> dict | None:
        """Structured completion: schema-enforced on primary, lenient parse on fallback."""
        result = await self.complete(tier, system, user_content, max_tokens=max_tokens,
                                     json_schema=schema, use_cache=False)
        return extract_json(result.text)

    async def _fallback_complete(self, tier: str, system: str, messages: list,
                                 max_tokens: int, json_schema: dict | None,
                                 timeout: float | None, cause: Exception | None,
                                 chain: list[str] | None = None) -> AIResult:
        client = self.fallback
        if client is None:
            raise (self._wrap(cause) if cause is not None
                   else AIError("cross-vendor model override requires OPENROUTER_API_TOKEN"))
        chain = chain or self.cfg.fallback_chain(tier)
        if json_schema:
            system = (f"{system}\n\nRespond with ONLY valid JSON matching this schema, "
                      f"no prose:\n{json.dumps(json_schema)}")
        request: dict[str, Any] = {
            "model": chain[0], "system": system, "messages": messages,
            "max_tokens": max_tokens,
            # cross-vendor degradation in one request; billed for the model that serves
            "extra_body": {"models": chain},
        }
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

    # --- agentic loop (investigator) ---

    async def agent_loop(
        self,
        tier: str,
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
        handlers = {tool.name: tool.handler for tool in tools}
        api_tools = [tool.to_api() for tool in tools]
        model = overrides.model_for_tier(tier, self.cfg)
        via_openrouter = "/" in model  # vendor-prefixed runtime override
        # cache the static prefix (tools + system + the MR diff/context render
        # ahead of it): every iteration re-sends it, so without this the whole
        # investigation is billed at full input price on each turn
        first_turn: dict = {"type": "text", "text": user_content}
        if not via_openrouter:
            first_turn["cache_control"] = dict(CACHE_CONTROL)
        messages: list[dict] = [{"role": "user", "content": [first_turn]}]
        rolling_cache: dict | None = None  # ≤4 breakpoints/request: keep one
        total_in = total_out = total_cr = total_cc = 0
        last_text = ""
        provider = "gateway"

        for iteration in range(max_iterations):
            await self._rate_limit()
            if via_openrouter:
                if self.fallback is None:
                    self._record_agent_usage(tier, model, provider, total_in, total_out, total_cr, total_cc)
                    raise AIError(
                        "cross-vendor model override requires OPENROUTER_API_TOKEN")
                request = {"model": model, "system": system,
                           "messages": _strip_thinking(_strip_cache_control(messages)),
                           "max_tokens": max_tokens, "tools": api_tools,
                           "extra_body": {"models": [model] + [
                               m for m in self.cfg.fallback_chain(tier) if m != model]}}
            else:
                request = {
                    "model": model, "system": system, "messages": messages,
                    "max_tokens": max_tokens, "tools": api_tools,
                    **self._primary_params(tier, effort, model),
                }
            try:
                target = self.fallback if via_openrouter else self.primary
                response = await self._call(target, request, self.cfg.ai_agent_timeout)
                provider = "openrouter" if via_openrouter else "gateway"
            except _RETRYABLE + (anthropic.APIStatusError,) as exc:
                if not _should_fallback(exc):
                    self._record_agent_usage(tier, model, provider, total_in, total_out, total_cr, total_cc)
                    raise self._wrap(exc)
                logger.warning("agent_loop primary failed (%s), trying openrouter",
                               type(exc).__name__)
                client = self.fallback
                if client is None:
                    self._record_agent_usage(tier, model, provider, total_in, total_out, total_cr, total_cc)
                    raise self._wrap(exc)
                chain = self.cfg.fallback_chain(tier)
                request = {"model": chain[0], "system": system,
                           "messages": _strip_thinking(_strip_cache_control(messages)),
                           "max_tokens": max_tokens, "tools": api_tools,
                           "extra_body": {"models": chain}}
                try:
                    response = await self._call(client, request, self.cfg.ai_agent_timeout)
                    provider = "openrouter"
                except Exception as exc2:  # noqa: BLE001
                    self._record_agent_usage(tier, model, provider, total_in, total_out, total_cr, total_cc)
                    raise self._wrap(exc2) from exc

            usage_info = getattr(response, "usage", None)
            total_in += getattr(usage_info, "input_tokens", 0) or 0
            total_out += getattr(usage_info, "output_tokens", 0) or 0
            total_cr += getattr(usage_info, "cache_read_input_tokens", 0) or 0
            total_cc += getattr(usage_info, "cache_creation_input_tokens", 0) or 0
            last_text = "".join(block.text for block in response.content
                                if getattr(block, "type", "") == "text") or last_text

            if response.stop_reason == "pause_turn":
                # Resume a paused turn: keep the FULL history and echo the paused
                # assistant content verbatim (thinking blocks included) — truncating
                # to messages[:1] would drop all prior tool_use/tool_result context.
                messages.append({"role": "assistant", "content": response.content})
                continue
            if response.stop_reason == "refusal":
                self._record_agent_usage(tier, model, provider, total_in, total_out, total_cr, total_cc)
                raise AIError("model refused during investigation (stop_reason=refusal)")
            if response.stop_reason != "tool_use":
                logger.info("agent_loop done after %d iterations stop=%s in=%d out=%d",
                            iteration + 1, response.stop_reason, total_in, total_out)
                self._record_agent_usage(tier, model, provider, total_in, total_out, total_cr, total_cc)
                return AIResult(text=last_text.strip(), model=model, provider=provider,
                                input_tokens=total_in, output_tokens=total_out,
                                cache_read_tokens=total_cr,
                                cache_creation_tokens=total_cc)

            messages.append({"role": "assistant", "content": response.content})
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
            if results and not via_openrouter:
                # roll the second breakpoint forward so each turn also reads the
                # growing history; drop the previous one (max 4 per request)
                if rolling_cache is not None:
                    rolling_cache.pop("cache_control", None)
                results[-1]["cache_control"] = dict(CACHE_CONTROL)
                rolling_cache = results[-1]
            messages.append({"role": "user", "content": results})

        logger.warning("agent_loop hit max_iterations=%d", max_iterations)
        self._record_agent_usage(tier, model, provider, total_in, total_out, total_cr, total_cc)
        return AIResult(text=last_text.strip(), model=model, provider=provider,
                        input_tokens=total_in, output_tokens=total_out,
                        cache_read_tokens=total_cr, cache_creation_tokens=total_cc)


ai_client = AIClient()
