"""Keep the suite hermetic: reviewer.config runs load_dotenv() on import, so a
developer's .env (e.g. AI_PROVIDER=openrouter) would leak into every Settings().
load_dotenv never overrides variables already set, so pinning them here wins."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
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
class World:
    gitlab: Any
    llm: Any
    telegram: Any
    repo: Any
    log_dir: Path
    client: Any
    responses: list[dict] = field(default_factory=list)

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
    on) wired to in-memory GitLab / LLM / Telegram / repo checkout."""
    from fastapi.testclient import TestClient

    from reviewer import gitlab_io, overrides, review_state, server, telegram_io
    from reviewer import pipeline as pipeline_mod
    from reviewer.config import settings
    from tests.fakes import FakeGitLab, FakeRepoCache, FakeTelegram, ScriptedLLM

    gitlab, llm, telegram = FakeGitLab(), ScriptedLLM(), FakeTelegram()
    repo = FakeRepoCache(gitlab, tmp_path / "repos")
    log_dir = tmp_path / "logs"

    for name, value in {
        "pipeline_v2": True, "investigator": False, "bridge_enabled": False,
        "tester_report": False, "review_repo_tools": True, "dialogue_enabled": True,
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

    # module-level caches of state files: drop them so every scenario starts
    # from its own empty STATE_DIR (no "already reviewed" sha, no overrides)
    monkeypatch.setattr(review_state, "_cache", None)
    monkeypatch.setattr(overrides, "_cache", None)

    monkeypatch.setattr(gitlab_io, "get_gitlab_client", gitlab.client)
    monkeypatch.setattr(telegram_io, "send_message", telegram.send_message)
    monkeypatch.setattr(telegram_io, "send_document", telegram.send_document)
    monkeypatch.setattr(pipeline_mod.pipeline, "ai", llm)
    monkeypatch.setattr(pipeline_mod.pipeline, "_dialogue_replies", {})
    monkeypatch.setattr(pipeline_mod, "repo_cache", repo)
    monkeypatch.setattr(server, "review_queue", server.ReviewQueue(
        workers=0, dedupe_ttl=600, burst_window=30))

    return World(gitlab, llm, telegram, repo, log_dir, TestClient(server.app))
