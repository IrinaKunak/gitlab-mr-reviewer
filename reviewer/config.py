"""Configuration: env loading, GitLab instances, model tiers, feature flags.

Env var names are kept compatible with v1 (.env / docker-compose contracts).
Legacy GEMINI_* knobs are read as fallbacks for their AI_* successors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _int(name: str, default: int, legacy: str | None = None) -> int:
    raw = os.getenv(name) or (os.getenv(legacy) if legacy else None)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float, legacy: str | None = None) -> float:
    raw = os.getenv(name) or (os.getenv(legacy) if legacy else None)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def load_gitlab_instances() -> dict[str, dict]:
    """Instances keyed by webhook token (X-Gitlab-Token routing key), as in v1."""
    instances: dict[str, dict] = {}
    url, token, webhook_token = (
        os.getenv("GITLAB_URL"), os.getenv("GITLAB_TOKEN"), os.getenv("XGITLABTOKEN"),
    )
    if url and token and webhook_token:
        instances[webhook_token] = {"url": url, "token": token, "name": "primary"}
    for i in range(2, 11):
        url = os.getenv(f"GITLAB_URL_{i}")
        token = os.getenv(f"GITLAB_TOKEN_{i}")
        webhook_token = os.getenv(f"XGITLABTOKEN_{i}")
        if url and token and webhook_token:
            instances[webhook_token] = {"url": url, "token": token, "name": f"instance_{i}"}
    return instances


@dataclass
class Settings:
    # --- GitLab ---
    gitlab_instances: dict[str, dict] = field(default_factory=load_gitlab_instances)
    review_for_conflict: bool = field(default_factory=lambda: _bool("REVIEW_FOR_CONFLICT"))
    review_language: str = field(default_factory=lambda: os.getenv("REVIEW_LANGUAGE", "en").lower())

    # --- AI provider ---
    ai_provider: str = field(default_factory=lambda: os.getenv("AI_PROVIDER", "anthropic").lower())
    anthropic_api_url: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_URL", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    anthropic_gateway_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY_GATEWAY", ""))
    openrouter_token: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_TOKEN", ""))
    model_fast: str = field(default_factory=lambda: os.getenv("ANTHROPIC_FAST_MODEL", "claude-haiku-4-5"))
    model_main: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MAIN_MODEL", "claude-sonnet-5"))
    model_smart: str = field(default_factory=lambda: os.getenv("ANTHROPIC_SMART_MODEL", "claude-opus-5"))
    fallback_fast: list[str] = field(default_factory=lambda: _csv(
        "OPENROUTER_FALLBACK_FAST",
        "anthropic/claude-haiku-4.5,google/gemini-3.5-flash-lite,deepseek/deepseek-v4-flash"))
    fallback_main: list[str] = field(default_factory=lambda: _csv(
        "OPENROUTER_FALLBACK_MAIN",
        "anthropic/claude-sonnet-5,google/gemini-3.6-flash,deepseek/deepseek-v4-pro"))
    fallback_smart: list[str] = field(default_factory=lambda: _csv(
        "OPENROUTER_FALLBACK_SMART",
        "anthropic/claude-opus-5,openai/gpt-5.6-terra,moonshotai/kimi-k3"))

    # --- AI behavior (legacy GEMINI_* read as fallback) ---
    ai_cache_ttl: int = field(default_factory=lambda: _int("AI_CACHE_TTL", 3600, "GEMINI_CACHE_TTL"))
    ai_cache_dir: str = field(default_factory=lambda: os.getenv(
        "AI_CACHE_DIR", os.getenv("GEMINI_CACHE_DIR", "cache")))
    ai_timeout: int = field(default_factory=lambda: _int("AI_TIMEOUT", 300, "GEMINI_TIMEOUT"))
    ai_agent_timeout: int = field(default_factory=lambda: _int("AI_AGENT_TIMEOUT", 600))
    ai_rate_limit: float = field(default_factory=lambda: _float(
        "AI_RATE_LIMIT", 2.0, "GEMINI_RATE_LIMIT"))
    ai_debug: bool = field(default_factory=lambda: _bool("AI_DEBUG", _bool("GEMINI_DEBUG")))
    ai_log_dir: str = field(default_factory=lambda: os.getenv(
        "AI_LOG_DIR", os.getenv("GEMINI_LOG_DIR", "logs")))
    # every current tier model has a 1M context window — 150k was a v1/Gemini-era
    # holdover that refused real MRs outright ("MR too large to analyze")
    ai_max_input_tokens: int = field(default_factory=lambda: _int("AI_MAX_INPUT_TOKENS", 300_000))
    ai_workers: int = field(default_factory=lambda: _int("AI_WORKERS", 2))
    dedupe_ttl: int = field(default_factory=lambda: _int("DEDUPE_TTL", 600))
    dedupe_burst: int = field(default_factory=lambda: _int("DEDUPE_BURST_SECONDS", 30))

    # legacy single-model review prompt overrides (used in parity mode / checklist content)
    review_prompt_en: str = field(default_factory=lambda: os.getenv("GEMINI_PROMPT", ""))
    review_prompt_ru: str = field(default_factory=lambda: os.getenv("GEMINI_PROMPT_RU", ""))

    # --- feature flags (per rollout phase) ---
    pipeline_v2: bool = field(default_factory=lambda: _bool("PIPELINE_V2"))
    investigator: bool = field(default_factory=lambda: _bool("INVESTIGATOR"))
    bridge_enabled: bool = field(default_factory=lambda: _bool("BRIDGE"))
    tester_report: bool = field(default_factory=lambda: _bool("TESTER_REPORT"))

    # --- repo cache ---
    repo_cache_dir: str = field(default_factory=lambda: os.getenv("REPO_CACHE_DIR", "repos"))
    repo_cache_max_gb: float = field(default_factory=lambda: _float("REPO_CACHE_MAX_GB", 30.0))
    # clone -> investigate -> remove (for small disks); off = LRU cache within the budget
    repo_cache_ephemeral: bool = field(default_factory=lambda: _bool("REPO_CACHE_EPHEMERAL"))
    investigator_max_iterations: int = field(
        default_factory=lambda: _int("INVESTIGATOR_MAX_ITERATIONS", 30))

    # --- Telegram ---
    telegram_enabled: bool = field(default_factory=lambda: os.getenv("TELEGRAM", "off").lower() == "on")
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_ids: list[str] = field(default_factory=lambda: [
        chat_id for chat_id in
        [os.getenv("TELEGRAM_CHAT_ID", "")] + [os.getenv(f"TELEGRAM_CHAT_ID_{i}", "") for i in range(1, 11)]
        if chat_id
    ])

    # --- Review Bridge ---
    bridge_chat_id: str = field(default_factory=lambda: os.getenv("REVIEW_BRIDGE_CHAT_ID", ""))
    # where tester reports are sent besides the bridge chat; empty = all
    # regular notification channels (TELEGRAM_CHAT_ID*)
    tester_report_chat_ids: list[str] = field(
        default_factory=lambda: _csv("TESTER_REPORT_CHAT_IDS", ""))
    # bearer token for GET /stats; empty = direct local access only
    stats_token: str = field(default_factory=lambda: os.getenv("STATS_TOKEN", ""))
    # HTTP Basic credentials for /stats & /dashboard (browser login prompt)
    stats_user: str = field(default_factory=lambda: os.getenv("STATS_USER", ""))
    stats_password: str = field(default_factory=lambda: os.getenv("STATS_PASSWORD", ""))
    # measured AIManager latency (2026-07-24): 49s, 73s, 81s, ~180s. 90s dropped
    # answers that were still coming — the cost of waiting is latency, not money
    bridge_question_timeout: int = field(default_factory=lambda: _int("BRIDGE_QUESTION_TIMEOUT", 240))
    bridge_answer_grace: float = field(default_factory=lambda: _float("BRIDGE_ANSWER_GRACE", 6.0))
    bridge_max_questions_per_mr: int = field(
        default_factory=lambda: _int("BRIDGE_MAX_QUESTIONS_PER_MR", 10))
    bridge_rate_per_hour: int = field(default_factory=lambda: _int("BRIDGE_RATE_PER_HOUR", 25))
    bridge_answer_bot_id: str = field(default_factory=lambda: os.getenv("BRIDGE_ANSWER_BOT_ID", ""))

    # --- network ---
    http_proxy: str = field(default_factory=lambda: os.getenv("HTTP_PROXY", ""))
    socks_proxy: str = field(default_factory=lambda: os.getenv("SOCKS_PROXY", ""))
    debug: bool = field(default_factory=lambda: _bool("DEBUG"))

    @property
    def proxy_url(self) -> str | None:
        """Proxy URL for httpx clients. HTTP proxy takes precedence (v1 behavior)."""
        if self.http_proxy:
            return self.http_proxy
        if self.socks_proxy:
            return f"socks5://{self.socks_proxy}"
        return None

    @property
    def requests_proxies(self) -> dict | None:
        """Proxies dict for requests-based clients (python-gitlab)."""
        if self.http_proxy:
            return {"http": self.http_proxy, "https": self.http_proxy}
        if self.socks_proxy:
            url = f"socks5h://{self.socks_proxy}"
            return {"http": url, "https": url}
        return None

    def model_for_tier(self, tier: str) -> str:
        return {"fast": self.model_fast, "main": self.model_main, "smart": self.model_smart}[tier]

    def fallback_chain(self, tier: str) -> list[str]:
        return {"fast": self.fallback_fast, "main": self.fallback_main, "smart": self.fallback_smart}[tier]


settings = Settings()
