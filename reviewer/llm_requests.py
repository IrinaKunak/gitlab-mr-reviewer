"""What goes on the wire: routing and request bodies for the Messages API.

One RequestBuilder serves `complete` and `agent_loop` on both routes:
  gateway     — Anthropic via Cloudflare AI Gateway; thinking/effort per tier
                from the model table (config `llm.models`), cache_control kept;
  openrouter  — Anthropic-compatible /api/v1/messages with a `models` array
                (same-class Claude first, then cross-vendor); thinking blocks
                stripped, cache_control kept only for anthropic/* heads.

The thinking policy itself (CLAUDE.md "Model & thinking policy"):
  smart — adaptive thinking + the caller's effort (the investigator thinks);
  main  — thinking OFF in the model's own way (disabled | between_tools), no
          effort; models with no off mode get adaptive at effort "low";
  fast  — no thinking param (haiku 400s on it; omitted = off there).
Models that reject the thinking param altogether (haiku) get nothing on any tier.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .domain.models import Tier

# Anthropic prompt caching is OPT-IN: without a cache_control breakpoint every
# agent-loop iteration re-bills the whole repo/diff prefix at full price. (The
# auto-caching models we reach through OpenRouter do this for us — that asymmetry
# is why terra investigations cost ~half what the same loop costs on the gateway.)
CACHE_CONTROL = {"type": "ephemeral"}  # 5-min TTL: reads 0.1x, writes 1.25x

# OpenRouter rejects a longer models array with a 400 ("'models' array must have
# 3 items or fewer") — prepending a runtime override to a 3-entry chain hits it
MAX_OPENROUTER_MODELS = 3

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")


def routing_chain(model: str, chain: list[str]) -> list[str]:
    """Preferred model first, then its fallbacks, deduped and length-capped."""
    ordered = [model] + [m for m in chain if m != model] if model else list(chain)
    return ordered[:MAX_OPENROUTER_MODELS]


def uses_openrouter(provider: str, model: str) -> bool:
    """OpenRouter is the route when chosen explicitly, or when the model id is
    vendor-prefixed (the CF gateway cannot serve openai/gpt-*, google/gemini-*)."""
    return provider == "openrouter" or "/" in model


def openrouter_model(model: str, chain: list[str]) -> str:
    """Id to send to OpenRouter. A slash id is used as-is; a plain Claude id
    (AI_PROVIDER=openrouter with the default ANTHROPIC_*_MODEL) maps to the
    head of that tier's OPENROUTER_FALLBACK_* chain."""
    if "/" in model:
        return model
    return chain[0] if chain else model


def wants_cache_control(via_openrouter: bool, model: str) -> bool:
    """Explicit cache breakpoints: always on the gateway, and on OpenRouter for
    anthropic/* — Claude does NOT auto-cache there (only OpenAI/Gemini/DeepSeek
    do), so stripping the markers billed every agent turn at full price
    (!493 via AI_PROVIDER=openrouter: 1.57M input, 0 cached, $6.05)."""
    return not via_openrouter or model.startswith("anthropic/")


def strip_cache_control(messages: list[dict]) -> list[dict]:
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


def strip_thinking(messages: list[dict]) -> list[dict]:
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


def openrouter_messages(messages: list[dict], model: str) -> list[dict]:
    """Messages as sent to OpenRouter: thinking blocks always dropped,
    cache_control kept only when the head model needs explicit caching."""
    if not wants_cache_control(True, model):
        messages = strip_cache_control(messages)
    return strip_thinking(messages)


@dataclass(frozen=True)
class Route:
    """Where a tier's requests go: the gateway with one model id, or OpenRouter
    with a models array (`chain`, head = `model`)."""
    model: str
    via_openrouter: bool = False
    chain: list[str] = field(default_factory=list)

    @property
    def cache_control(self) -> bool:
        return wants_cache_control(self.via_openrouter, self.model)


class RequestBuilder:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg

    # --- routing ---

    def route(self, tier: Tier, model: str) -> Route:
        if not uses_openrouter(self.cfg.llm.provider, model):
            return Route(model)
        chain = self.cfg.fallback_chain(tier)
        model = openrouter_model(model, chain)
        return Route(model, via_openrouter=True, chain=routing_chain(model, chain))

    def fallback_chain(self, tier: Tier) -> list[str]:
        """The tier's OpenRouter chain, used when the gateway is unavailable."""
        return routing_chain("", self.cfg.fallback_chain(tier))

    # --- thinking / effort ---

    def thinking_params(self, tier: Tier, model: str, effort: str | None) -> dict[str, Any]:
        """Thinking/effort config valid for this gateway model on this tier."""
        spec = self.cfg.llm.model_spec(model)
        if not spec.supports_thinking:
            return {}
        if tier == Tier.SMART:
            # only the investigator thinks: on big-diff reviews adaptive thinking
            # ate the entire max_tokens budget before emitting any text (prod,
            # 2026-07-22) while the non-thinking fallback wrote a great review
            params: dict[str, Any] = {"thinking": {"type": "adaptive"}}
            if effort and spec.supports_effort:
                params["output_config"] = {"effort": effort}
            return params
        if tier == Tier.MAIN:
            if spec.thinking_off == "none":
                params = {"thinking": {"type": "adaptive"}}
                if spec.supports_effort:
                    params["output_config"] = {"effort": "low"}
                return params
            params = {"thinking": {"type": spec.thinking_off}}
            cap = spec.max_effort_with_thinking_off
            if (effort and cap and effort in EFFORT_ORDER
                    and EFFORT_ORDER.index(effort) <= EFFORT_ORDER.index(cap)):
                params["output_config"] = {"effort": effort}
            return params
        # fast: omitted param = no thinking on haiku; no effort (400s on haiku)
        return {}

    # --- bodies ---

    def gateway(self, tier: Tier, model: str, system: str, messages: list[dict],
                max_tokens: int, *, effort: str | None = None,
                tools: list[dict] | None = None, json_schema: dict | None = None) -> dict:
        request: dict[str, Any] = {"model": model, "system": system, "messages": messages,
                                   "max_tokens": max_tokens}
        if tools is not None:
            request["tools"] = tools
        request.update(self.thinking_params(tier, model, effort))
        if json_schema:
            request["output_config"] = {
                **request.get("output_config", {}),
                "format": {"type": "json_schema", "schema": json_schema},
            }
        return request

    def openrouter(self, chain: list[str], system: str, messages: list[dict],
                   max_tokens: int, *, tools: list[dict] | None = None,
                   json_schema: dict | None = None) -> dict:
        """No thinking params: cross-vendor models may reject Anthropic-only
        fields; the `models` array does the failover in one request (billed for
        the model that serves)."""
        if json_schema:
            system = (f"{system}\n\nRespond with ONLY valid JSON matching this schema, "
                      f"no prose:\n{json.dumps(json_schema)}")
        request: dict[str, Any] = {
            "model": chain[0], "system": system,
            "messages": openrouter_messages(messages, chain[0]),
            "max_tokens": max_tokens, "extra_body": {"models": chain},
        }
        if tools is not None:
            request["tools"] = tools
        return request
