"""Scripted stand-in for AIClient.

Answers come from per-method queues, in call order:
  - `complete` (also feeds `complete_json`, like the real client) — str,
    dict (JSON answer), or an Exception instance to raise;
  - `agent_loop` — AgentScript (tool calls to run, then final text) or an
    Exception instance.
An exhausted queue raises AssertionError: an unexpected AI call is a test
failure, not a silent default. Every call is recorded with its tier and the
model that tier resolves to, and reported to usage like the real client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from reviewer import usage
from reviewer.ai_client import AIInputTooLargeError, AIResult, ToolDef, estimate_tokens
from reviewer.config import Settings


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class AgentScript:
    final_text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class LLMCall:
    method: str          # complete | complete_json | agent_loop
    tier: str
    model: str
    system: str
    user: str
    tool_results: list[tuple[str, str]] = field(default_factory=list)


class ScriptedLLM:
    def __init__(self, settings: Settings) -> None:
        # read live: a scenario may change the budget after the graph is built
        self.settings = settings
        self.complete_script: list[Any] = []
        self.agent_script: list[Any] = []
        self.calls: list[LLMCall] = []

    # --- scripting ---

    def on_complete(self, *answers: Any) -> ScriptedLLM:
        self.complete_script.extend(answers)
        return self

    def on_agent(self, *scripts: Any) -> ScriptedLLM:
        self.agent_script.extend(scripts)
        return self

    def tiers(self, method: str | None = None) -> list[str]:
        return [c.tier for c in self.calls if method in (None, c.method)]

    # --- AIClient surface used by the pipeline ---

    def guard_input_size(self, *parts: str) -> None:
        total = sum(estimate_tokens(p) for p in parts)
        limit = self.settings.llm.max_input_tokens
        if total > limit:
            raise AIInputTooLargeError(f"input ~{total} tokens exceeds limit {limit}")

    async def complete(self, tier: str, system: str, user_content: str, *,
                       max_tokens: int = 4096, effort: str | None = None,
                       json_schema: dict | None = None, use_cache: bool = True,
                       timeout: float | None = None) -> AIResult:
        return self._answer("complete", tier, system, user_content)

    async def complete_json(self, tier: str, system: str, user_content: str,
                            schema: dict, *, max_tokens: int = 2048) -> dict | None:
        result = self._answer("complete_json", tier, system, user_content)
        return json.loads(result.text)

    async def agent_loop(self, tier: str, system: str, user_content: str,
                         tools: list[ToolDef], *, max_iterations: int = 30,
                         max_tokens: int = 16000, effort: str = "high") -> AIResult:
        self.guard_input_size(system, user_content)
        call = self._record("agent_loop", tier, system, user_content)
        script = self._next(self.agent_script, "agent_loop", call)
        by_name = {t.name: t for t in tools}
        for tc in script.tool_calls:
            assert tc.name in by_name, f"agent called unknown tool {tc.name}"
            call.tool_results.append((tc.name, await by_name[tc.name].handler(**tc.args)))
        return self._result(call, script.final_text)

    # --- internals ---

    def _answer(self, method: str, tier: str, system: str, user: str) -> AIResult:
        self.guard_input_size(system, user)
        call = self._record(method, tier, system, user)
        answer = self._next(self.complete_script, method, call)
        text = json.dumps(answer) if isinstance(answer, dict) else str(answer)
        return self._result(call, text)

    def _record(self, method: str, tier: str, system: str, user: str) -> LLMCall:
        call = LLMCall(method, tier, self.settings.model_for_tier(tier), system, user)
        self.calls.append(call)
        return call

    @staticmethod
    def _next(script: list[Any], method: str, call: LLMCall) -> Any:
        if not script:
            raise AssertionError(
                f"unscripted {method} call on tier {call.tier}: {call.system[:120]!r}")
        answer = script.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @staticmethod
    def _result(call: LLMCall, text: str) -> AIResult:
        result = AIResult(text=text, model=call.model, provider="scripted",
                          input_tokens=estimate_tokens(call.system + call.user),
                          output_tokens=estimate_tokens(text))
        usage.record(tier=call.tier, model=call.model, provider="scripted",
                     input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        return result
