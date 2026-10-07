"""In-memory fakes for the service's outer boundaries (GitLab, LLM, Telegram,
Review Bridge, repo checkout). Wired in by the `world` fixture in tests/conftest.py."""

from .bridge import FakeBridge
from .gitlab import FakeGitLab, file_change, mr_webhook, note_webhook
from .llm import AgentScript, ScriptedLLM, ToolCall
from .repo import FakeRepoCache
from .telegram import FakeTelegram

__all__ = ["AgentScript", "FakeBridge", "FakeGitLab", "FakeRepoCache", "FakeTelegram",
           "ScriptedLLM", "ToolCall", "file_change", "mr_webhook", "note_webhook"]
