"""Keep the suite hermetic: reviewer.config runs load_dotenv() on import, so a
developer's .env (e.g. AI_PROVIDER=openrouter) would leak into every Settings().
load_dotenv never overrides variables already set, so pinning them here wins."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ["AI_PROVIDER"] = "anthropic"


# --- characterization harness ------------------------------------------------
# The ONLY place that monkeypatches module boundaries for scenario tests. Once
# a composition root exists (refactoring stage 10) this fixture will hand the
# same fakes to the bootstrap instead, and the scenario tests stay unchanged.
# `reviewer` is imported inside the fixture only: the env pin above must run
# before reviewer.config's load_dotenv().

WEBHOOK_TOKEN = "hook-token"


@dataclass
class Clock:
    """Monotonic time as the webhook queue sees it (dedupe TTL, burst window)."""
    now: float = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class World:
    gitlab: Any
    llm: Any
    telegram: Any
    bridge: Any
    repo: Any
    clock: Clock
    log_dir: Path
    client: Any
    monkeypatch: Any
    responses: list[dict] = field(default_factory=list)

    def configure(self, **values: Any) -> None:
        """Scenario-specific settings on top of the prod-shaped defaults."""
        from reviewer.config import settings
        for name, value in values.items():
            assert hasattr(settings, name), f"unknown setting {name}"
            self.monkeypatch.setattr(settings, name, value)

    def send(self, payload: dict, event: str = "Merge Request Hook") -> dict:
        """POST a webhook, then run whatever it queued the way a worker does."""
        from reviewer import pipeline as pipeline_mod
        from reviewer import server

        resp = self.client.post("/webhook", json=payload, headers={
            "X-Gitlab-Token": WEBHOOK_TOKEN, "X-Gitlab-Event": event})
        body = {"status_code": resp.status_code, **resp.json()}
        self.responses.append(body)
        queue = server.review_queue.queue
        while not queue.empty():
            job = queue.get_nowait()
            if job.get("kind") == "note":
                asyncio.run(pipeline_mod.pipeline.process_note(job))
            else:
                asyncio.run(pipeline_mod.pipeline.process(job))
        return body

    def usage_entries(self) -> list[dict]:
        path = self.log_dir / "usage.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Production-shaped service (all v2 flags as deployed, RU output, Telegram
    on) wired to in-memory GitLab / LLM / Telegram / bridge / repo checkout."""
    from fastapi.testclient import TestClient

    from reviewer import gitlab_io, server, telegram_io
    from reviewer import pipeline as pipeline_mod
    from reviewer.config import settings
    from tests.fakes import FakeBridge, FakeGitLab, FakeRepoCache, FakeTelegram, ScriptedLLM

    gitlab, llm, telegram, bridge = FakeGitLab(), ScriptedLLM(), FakeTelegram(), FakeBridge()
    clock = Clock()
    repo = FakeRepoCache(gitlab, tmp_path / "repos")
    log_dir = tmp_path / "logs"

    for name, value in {
        "investigator": True, "bridge_enabled": True,
        "tester_report": True, "review_repo_tools": True, "dialogue_enabled": True,
        "bridge_chat_id": "bridge-chat", "tester_report_chat_ids": [],
        "dialogue_max_replies_per_mr": 20,
        "ai_provider": "anthropic", "review_language": "ru", "review_for_conflict": False,
        "review_prompt_en": "", "ai_max_input_tokens": 300_000,
        "telegram_enabled": True, "telegram_token": "test-token",
        "telegram_chat_ids": ["chat-1"],
        "state_dir": str(tmp_path / "state"), "ai_log_dir": str(log_dir),
        "ai_cache_dir": str(tmp_path / "cache"),
        "gitlab_instances": {WEBHOOK_TOKEN: {
            "name": "primary", "url": "https://gitlab.test", "token": "t",
            "bot_username": gitlab.bot_username}},
    }.items():
        monkeypatch.setattr(settings, name, value)

    monkeypatch.setattr(gitlab_io, "get_gitlab_client", gitlab.client)
    monkeypatch.setattr(telegram_io, "send_message", telegram.send_message)
    monkeypatch.setattr(telegram_io, "send_document", telegram.send_document)
    monkeypatch.setattr(pipeline_mod.pipeline, "ai", llm)
    monkeypatch.setattr(pipeline_mod.pipeline, "_dialogue_replies", {})
    monkeypatch.setattr(pipeline_mod, "repo_cache", repo)
    monkeypatch.setattr(pipeline_mod, "bridge", bridge)
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(server, "review_queue", server.ReviewQueue(
        workers=0, dedupe_ttl=600, burst_window=30))

    return World(gitlab, llm, telegram, bridge, repo, clock, log_dir,
                 TestClient(server.app), monkeypatch)
