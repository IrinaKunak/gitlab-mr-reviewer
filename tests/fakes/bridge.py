"""Scripted Review Bridge: AIManager's answers come from a queue, in order.

Replaces the `bridge` singleton the pipeline imports (same `enabled` / `ask`
surface), so the investigator's `ask_aimanager` tool runs for real against
it. `None` in the script is a timeout / "not found" answer. An exhausted
script raises AssertionError, like ScriptedLLM: an unexpected question is a
test failure.
"""

from __future__ import annotations

from typing import Any


class FakeBridge:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.script: list[Any] = []
        self.questions: list[str] = []

    def on_ask(self, *answers: str | None) -> FakeBridge:
        self.script.extend(answers)
        return self

    async def ask(self, question: str) -> str | None:
        self.questions.append(question)
        if not self.script:
            raise AssertionError(f"unscripted bridge question: {question[:120]!r}")
        return self.script.pop(0)
