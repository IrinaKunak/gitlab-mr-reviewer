"""In-memory fakes for the service's outer boundaries (GitLab, LLM, Telegram,
repo checkout). Wired in by the `world` fixture in tests/conftest.py."""

from .gitlab import FakeGitLab, file_change, mr_webhook
from .llm import AgentScript, ScriptedLLM, ToolCall
from .repo import FakeRepoCache
from .telegram import FakeTelegram

__all__ = ["AgentScript", "FakeGitLab", "FakeRepoCache", "FakeTelegram", "ScriptedLLM",
           "ToolCall", "file_change", "mr_webhook"]
