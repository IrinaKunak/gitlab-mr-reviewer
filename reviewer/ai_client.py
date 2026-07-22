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

    def _primary_params(self, tier: str, effort: str | None) -> dict:
        """Thinking/effort config valid for the primary Claude model of this tier."""
        params: dict[str, Any] = {}
        if tier in ("main", "smart"):
            params["thinking"] = {"type": "adaptive"}
            if effort:
                params["output_config"] = {"effort": effort}
        # fast = haiku-4-5: no thinking param, no effort (effort 400s on Haiku)
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
        model = self.cfg.model_for_tier(tier)

        cache_key = self._cache_key(model, system, user_content)
        if use_cache and not json_schema:
            cached = self._cache_get(cache_key)
            if cached is not None:
                logger.info("ai cache hit tier=%s", tier)
                return AIResult(text=cached, model=model, provider="cache", cached=True)

        await self._rate_limit()
        messages = [{"role": "user", "content": user_content}]
        self._debug("request", f"tier={tier} model={model} system={system[:500]} "
                               f"user={user_content[:2000]}")

        request: dict[str, Any] = {
            "model": model, "system": system, "messages": messages,
            "max_tokens": max_tokens, **self._primary_params(tier, effort),
        }
        if json_schema:
            request["output_config"] = {
                **request.get("output_config", {}),
                "format": {"type": "json_schema", "schema": json_schema},
            }

        started = time.monotonic()
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

        logger.info(
            "ai ok tier=%s model=%s provider=%s in=%d out=%d %.1fs",
            tier, result.model, result.provider, result.input_tokens,
            result.output_tokens, time.monotonic() - started)
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
                                 timeout: float | None, cause: Exception) -> AIResult:
        client = self.fallback
        if client is None:
            raise self._wrap(cause)
        chain = self.cfg.fallback_chain(tier)
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
        usage = getattr(response, "usage", None)
        return AIResult(
            text=text.strip(),
            model=getattr(response, "model", ""),
            provider=provider,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
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
        messages: list[dict] = [{"role": "user", "content": user_content}]
        model = self.cfg.model_for_tier(tier)
        total_in = total_out = 0
        last_text = ""

        for iteration in range(max_iterations):
            await self._rate_limit()
            request = {
                "model": model, "system": system, "messages": messages,
                "max_tokens": max_tokens, "tools": api_tools,
                **self._primary_params(tier, effort),
            }
            try:
                response = await self._call(self.primary, request, self.cfg.ai_agent_timeout)
                provider = "gateway"
            except _RETRYABLE + (anthropic.APIStatusError,) as exc:
                if not _should_fallback(exc):
                    raise self._wrap(exc)
                logger.warning("agent_loop primary failed (%s), trying openrouter",
                               type(exc).__name__)
                client = self.fallback
                if client is None:
                    raise self._wrap(exc)
                chain = self.cfg.fallback_chain(tier)
                request = {"model": chain[0], "system": system,
                           "messages": _strip_thinking(messages),
                           "max_tokens": max_tokens, "tools": api_tools,
                           "extra_body": {"models": chain}}
                try:
                    response = await self._call(client, request, self.cfg.ai_agent_timeout)
                    provider = "openrouter"
                except Exception as exc2:  # noqa: BLE001
                    raise self._wrap(exc2) from exc

            usage = getattr(response, "usage", None)
            total_in += getattr(usage, "input_tokens", 0) or 0
            total_out += getattr(usage, "output_tokens", 0) or 0
            last_text = "".join(block.text for block in response.content
                                if getattr(block, "type", "") == "text") or last_text

            if response.stop_reason == "pause_turn":
                # Resume a paused turn: keep the FULL history and echo the paused
                # assistant content verbatim (thinking blocks included) — truncating
                # to messages[:1] would drop all prior tool_use/tool_result context.
                messages.append({"role": "assistant", "content": response.content})
                continue
            if response.stop_reason == "refusal":
                raise AIError("model refused during investigation (stop_reason=refusal)")
            if response.stop_reason != "tool_use":
                logger.info("agent_loop done after %d iterations stop=%s in=%d out=%d",
                            iteration + 1, response.stop_reason, total_in, total_out)
                return AIResult(text=last_text.strip(), model=model, provider=provider,
                                input_tokens=total_in, output_tokens=total_out)

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
            messages.append({"role": "user", "content": results})

        logger.warning("agent_loop hit max_iterations=%d", max_iterations)
        return AIResult(text=last_text.strip(), model=model, provider="gateway",
                        input_tokens=total_in, output_tokens=total_out)


ai_client = AIClient()
