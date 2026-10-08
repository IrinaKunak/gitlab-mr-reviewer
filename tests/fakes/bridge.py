"""Scripted Review Bridge: AIManager's answers come from a queue, in order.

Stands in for ReviewBridge in build_services (the KnowledgeSource surface:
`enabled` / `ask` / `archive` / `start` / `stop`), so the investigator's `ask_aimanager` tool runs for real against
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
        self.archived: list[tuple[str, bytes, str]] = []  # (filename, content, caption)

    def on_ask(self, *answers: str | None) -> FakeBridge:
        self.script.extend(answers)
        return self

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def ask(self, question: str) -> str | None:
        self.questions.append(question)
        if not self.script:
            raise AssertionError(f"unscripted bridge question: {question[:120]!r}")
        return self.script.pop(0)

    async def archive(self, filename: str, content: bytes, caption: str = "") -> None:
        self.archived.append((filename, content, caption))
