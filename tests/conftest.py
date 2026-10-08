"""Keep the suite hermetic: nothing in `reviewer` reads .env on import any more
(bootstrap.load_config does, and tests never call it with dotenv), but a
developer's shell may export these — pin them so every Settings() is the same."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

os.environ["AI_PROVIDER"] = "anthropic"
# a developer's config.yaml must not leak in either; yaml tests point CONFIG_FILE at tmp
os.environ["CONFIG_FILE"] = "/nonexistent/config.yaml"


# --- characterization harness ------------------------------------------------
# Builds the real object graph through bootstrap.build_services, with fakes on
# the outer edges (GitLab, LLM, Telegram transport, bridge, repo checkout,
# clock). Scenario tests only see the `world` surface below.

WEBHOOK_TOKEN = "hook-token"


def set_setting(monkeypatch, settings, path: str, value: Any) -> None:
    """monkeypatch one nested setting by dotted path, e.g. "pipeline.stages.investigator"."""
    *parents, leaf = path.split(".")
    target = settings
    for name in parents:
        target = getattr(target, name)
    assert leaf in type(target).model_fields, f"unknown setting {path}"
    monkeypatch.setattr(target, leaf, value)


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
    settings: Any
    services: Any
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
        for name, value in values.items():  # llm__max_input_tokens=... -> llm.max_input_tokens
            set_setting(self.monkeypatch, self.settings, name.replace("__", "."), value)

    def send(self, payload: dict, event: str = "Merge Request Hook") -> dict:
        """POST a webhook, then run whatever it queued the way a worker does."""
        resp = self.client.post("/webhook", json=payload, headers={
            "X-Gitlab-Token": WEBHOOK_TOKEN, "X-Gitlab-Event": event})
        body = {"status_code": resp.status_code, **resp.json()}
        self.responses.append(body)
        queue = self.services.queue
        while not queue.queue.empty():
            asyncio.run(queue.run(queue.queue.get_nowait()))
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

    from reviewer.bootstrap import build_services
    from reviewer.config import Settings
    from reviewer.domain.models import InstanceRef
    from reviewer.server import create_app
    from tests.fakes import FakeBridge, FakeGitLab, FakeRepoCache, FakeTelegram, ScriptedLLM

    log_dir = tmp_path / "logs"
    cfg = Settings()
    for path, value in {
        "pipeline.stages.investigator": True, "bridge.enabled": True,
        "pipeline.stages.tester_report": True, "pipeline.stages.review_repo_tools": True,
        "pipeline.stages.dialogue": True,
        "bridge.chat_id": "bridge-chat", "notify.telegram.tester_report_chat_ids": [],
        "pipeline.dialogue_max_replies_per_mr": 20,
        "llm.provider": "anthropic", "pipeline.language": "ru",
        "pipeline.review_for_conflict": False,
        "pipeline.review_prompt": "", "llm.max_input_tokens": 300_000,
        "notify.telegram.enabled": True, "notify.telegram.token": "test-token",
        "notify.telegram.chat_ids": ["chat-1"],
        "storage.state_dir": str(tmp_path / "state"), "storage.log_dir": str(log_dir),
        "storage.ai_cache_dir": str(tmp_path / "cache"),
        "gitlab.routes": {WEBHOOK_TOKEN: InstanceRef("primary", "https://gitlab.test", "t")},
    }.items():
        set_setting(monkeypatch, cfg, path, value)

    gitlab, llm, bridge = FakeGitLab(), ScriptedLLM(cfg), FakeBridge()
    telegram = FakeTelegram(cfg.notify.telegram, language=cfg.pipeline.language)
    clock = Clock()
    repo = FakeRepoCache(gitlab, tmp_path / "repos")
    services = build_services(cfg, telegram=telegram, ai=llm, bridge=bridge,
                              repo_cache=repo, vcs_for=lambda instance: gitlab,
                              clock=clock.monotonic, workers=0)

    return World(cfg, services, gitlab, llm, telegram, bridge, repo, clock, log_dir,
                 TestClient(create_app(services)), monkeypatch)
