"""Configuration: typed sections, validated at startup (pydantic-settings).

Sources, highest priority first:
  1. env vars (bootstrap.load_config loads the .env file into the environment) —
     the flat names from v1 (ANTHROPIC_MAIN_MODEL, INVESTIGATOR, …) stay the
     canonical way to set scalars and secrets, see ENV_FIELDS;
  2. optional `config.yaml` (CONFIG_FILE) for structured data — GitLab
     instances, Telegram channels, model prices. String values may reference
     env vars as ${NAME} / ${NAME:-default}, so secrets never live in the file;
  3. defaults below.

A bad value (INVESTIGATOR=maybe, AI_TIMEOUT=abc, an unknown tier or section in
config.yaml, an instance without a token) stops the service at startup with a
message naming the env var — v1 silently fell back to the default instead.

Legacy numbered lists (GITLAB_URL[_N]/GITLAB_TOKEN[_N]/XGITLABTOKEN[_N],
TELEGRAM_CHAT_ID[_N]) still work but log a deprecation warning: move them to
config.yaml / TELEGRAM_CHAT_IDS. GEMINI_* and PIPELINE_V2 were retired in
refactoring stage 6 (RETIRED_ENV_VARS).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .domain.models import InstanceRef, Tier

TIERS = tuple(Tier)
LEGACY_INSTANCE_SLOTS = ("", *(f"_{i}" for i in range(2, 11)))  # "" = primary
LEGACY_CHAT_SLOTS = ("", *(f"_{i}" for i in range(1, 11)))


class _Section(BaseModel):
    # a typo in config.yaml (or an unknown tier) is an error, not a silent no-op
    model_config = ConfigDict(extra="forbid")


def _csv(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


# --- gitlab ---------------------------------------------------------------

class GitLabInstance(_Section):
    # the name is part of the incremental-review state key (review_state):
    # keep "primary" / "instance_N" when moving env instances to config.yaml
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    token: str = Field(min_length=1)
    webhook_token: str = Field(min_length=1)  # X-Gitlab-Token routing key


class GitLabSection(_Section):
    instances: list[GitLabInstance] = []
    # GITLAB_URL[_N] trios; used only when `instances` is empty (config.yaml wins)
    legacy_instances: list[GitLabInstance] = Field(default=[], exclude=True)
    # webhook token -> instance (X-Gitlab-Token routing); immutable — the
    # bot's username per instance is runtime state kept by the server
    routes: dict[str, InstanceRef] = Field(default={}, exclude=True)

    @model_validator(mode="after")
    def _build_routes(self) -> GitLabSection:
        if not self.instances:
            self.instances = list(self.legacy_instances)
        names = [i.name for i in self.instances]
        hooks = [i.webhook_token for i in self.instances]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate GitLab instance names: {names}")
        if len(set(hooks)) != len(hooks):
            raise ValueError("two GitLab instances share a webhook token")
        self.routes = {i.webhook_token: InstanceRef(i.name, i.url, i.token)
                       for i in self.instances}
        return self


# --- llm ------------------------------------------------------------------

class TierConfig(_Section):
    model: str
    fallback: list[str]  # OpenRouter models array, head first

    _split = field_validator("fallback", mode="before")(_csv)


DEFAULT_TIERS: dict[str, dict[str, Any]] = {
    Tier.FAST: {"model": "claude-haiku-4-5",
             "fallback": ["anthropic/claude-haiku-4.5", "google/gemini-3.5-flash-lite",
                          "deepseek/deepseek-v4-flash"]},
    Tier.MAIN: {"model": "claude-sonnet-5",
             "fallback": ["anthropic/claude-sonnet-5", "google/gemini-3.6-flash",
                          "deepseek/deepseek-v4-pro"]},
    Tier.SMART: {"model": "claude-opus-5",
              "fallback": ["anthropic/claude-opus-5", "openai/gpt-5.6-terra",
                           "moonshotai/kimi-k3"]},
}


class TiersSection(_Section):
    fast: TierConfig = TierConfig(**DEFAULT_TIERS["fast"])
    main: TierConfig = TierConfig(**DEFAULT_TIERS["main"])
    smart: TierConfig = TierConfig(**DEFAULT_TIERS["smart"])

    @model_validator(mode="before")
    @classmethod
    def _tier_defaults(cls, data: Any) -> Any:
        """Setting only a tier's model keeps its default fallback chain (and back)."""
        if not isinstance(data, dict):
            return data
        return {**data, **{tier: {**DEFAULT_TIERS[tier], **value}
                           for tier, value in data.items()
                           if tier in DEFAULT_TIERS and isinstance(value, dict)}}

    def get(self, tier: Tier | str) -> TierConfig:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r} (expected one of "
                             f"{[t.value for t in TIERS]})")
        return getattr(self, Tier(tier).value)


class ModelSpec(_Section):
    """What one model accepts — decides the thinking/effort params per tier
    (ai_client.RequestBuilder), so a new model is a config entry, not a code
    release. Keys of `llm.models` are model ids or id prefixes; the longest key
    that equals the id or prefixes it at a "-" wins (claude-sonnet-5-5 beats
    claude-sonnet-5; dated ids like claude-haiku-4-5-20251001 match their alias)."""
    # False: the `thinking` param itself is a 400 (haiku) — nothing is sent
    supports_thinking: bool = True
    # how the main tier turns thinking off: "disabled" | "between_tools" (sonnet-5-5:
    # no other thinking field allowed) | "none" (no off mode at all: adaptive at
    # effort low is the only lever that keeps it from eating the output budget)
    thinking_off: Literal["disabled", "between_tools", "none"] = "disabled"
    supports_effort: bool = True
    # highest effort legal alongside the thinking-off mode; None = never send
    # effort with thinking off (the main tier's policy — and opus-5's disabled
    # mode is a 400 at xhigh/max, sonnet-5-5's between_tools at xhigh/max)
    max_effort_with_thinking_off: Literal["low", "medium", "high"] | None = None
    price: tuple[float, float] | None = None  # $/MTok (input, output), list price


def _spec(**kw: Any) -> dict[str, Any]:
    return kw


# Hard-won (CLAUDE.md "Model & thinking policy"); yaml `llm.models` entries
# override or extend these per key. OpenRouter ids only carry prices: thinking
# params are sent on the gateway path only.
DEFAULT_MODELS: dict[str, dict[str, Any]] = {
    # haiku: any thinking param or effort is a 400
    "claude-haiku": _spec(supports_thinking=False, supports_effort=False),
    "claude-haiku-4-5": _spec(supports_thinking=False, supports_effort=False, price=(1.0, 5.0)),
    # sonnet-5 runs ADAPTIVE thinking when the param is omitted (unlike 4-6):
    # disabling must be explicit, else thinking eats max_tokens on big diffs
    "claude-sonnet-5": _spec(thinking_off="disabled", price=(2.0, 10.0)),  # intro price to 2026-08-31
    # sonnet-5-5 400s on "disabled" (prod 2026-09-29, !127)
    "claude-sonnet-5-5": _spec(thinking_off="between_tools", price=(2.0, 10.0)),
    "claude-sonnet-4-6": _spec(thinking_off="disabled", price=(3.0, 15.0)),
    "claude-opus-4-8": _spec(thinking_off="disabled", price=(5.0, 25.0)),
    # disabled thinking is a 400 at effort xhigh/max (fine without effort)
    "claude-opus-5": _spec(thinking_off="disabled", price=(5.0, 25.0)),
    # no thinking-off mode at all
    "claude-opus-5-5": _spec(thinking_off="none", price=(4.0, 20.0)),
    "claude-fable": _spec(thinking_off="none"),
    "claude-fable-5": _spec(thinking_off="none", price=(10.0, 50.0)),
    "claude-mythos": _spec(thinking_off="none"),
    "anthropic/claude-haiku-4.5": _spec(price=(1.0, 5.0)),
    "anthropic/claude-sonnet-5": _spec(price=(2.0, 10.0)),
    "anthropic/claude-sonnet-5.5": _spec(price=(2.0, 10.0)),
    "anthropic/claude-opus-4.8": _spec(price=(5.0, 25.0)),
    "anthropic/claude-opus-5": _spec(price=(5.0, 25.0)),
    "google/gemini-3.6-flash": _spec(price=(1.5, 7.5)),
    "google/gemini-3.5-flash-lite": _spec(price=(0.3, 2.5)),
    "deepseek/deepseek-v4-flash": _spec(price=(0.1, 0.2)),
    "deepseek/deepseek-v4-pro": _spec(price=(0.43, 0.87)),
    "openai/gpt-5.6-terra": _spec(price=(2.5, 15.0)),
    "moonshotai/kimi-k3": _spec(price=(3.0, 15.0)),
}


def match_model_key(model: str, keys: Any) -> str | None:
    """Longest key equal to `model` or prefixing it at a "-" boundary."""
    best = None
    for key in keys:
        if (model == key or model.startswith(key + "-")) and len(key) > len(best or ""):
            best = key
    return best


class AnthropicSection(_Section):
    api_url: str = ""  # CF AI Gateway /anthropic route
    api_key: str = ""  # real key, x-api-key
    gateway_key: str = ""  # cfut_..., cf-aig-authorization: Bearer


class OpenRouterSection(_Section):
    token: str = ""


class LLMSection(_Section):
    provider: Literal["anthropic", "openrouter"] = "anthropic"
    anthropic: AnthropicSection = AnthropicSection()
    openrouter: OpenRouterSection = OpenRouterSection()
    tiers: TiersSection = TiersSection()
    # model capability + price table (DEFAULT_MODELS, extended by config.yaml)
    models: dict[str, ModelSpec] = Field(default_factory=lambda: {
        k: ModelSpec(**v) for k, v in DEFAULT_MODELS.items()})
    # $/MTok (input, output) overrides on top of the table's prices (MODEL_PRICES)
    prices: dict[str, tuple[float, float]] = {}
    cache_ttl: int = Field(default=3600, ge=0)
    # agent loops reading at least this many input tokens with zero cache reads
    # get a WARNING + Telegram alert (0 disables): a silently broken prompt
    # cache re-bills the whole prefix every turn
    cache_alert_min_input: int = Field(default=100_000, ge=0)
    timeout: int = Field(default=300, gt=0)  # timed-out calls still bill server-side
    agent_timeout: int = Field(default=600, gt=0)
    rate_limit: float = Field(default=2.0, ge=0)
    debug: bool = False
    # every current tier model has a 1M context window — 150k was a v1/Gemini-era
    # holdover that refused real MRs outright ("MR too large to analyze")
    max_input_tokens: int = Field(default=300_000, gt=0)

    @field_validator("provider", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("models", mode="before")
    @classmethod
    def _merge_models(cls, value: Any) -> Any:
        """config.yaml entries override/extend the built-in table per key."""
        if not isinstance(value, dict):
            return value
        return {**DEFAULT_MODELS, **value}

    def model_spec(self, model: str) -> ModelSpec:
        """Capabilities for a model id; unknown ids get ModelSpec() — thinking
        disabled explicitly on the main tier, as for any recent Claude."""
        key = match_model_key(model, self.models)
        return self.models[key] if key is not None else ModelSpec()

    def price_table(self) -> dict[str, tuple[float, float]]:
        """Table prices with MODEL_PRICES / llm.prices on top."""
        table = {k: spec.price for k, spec in self.models.items() if spec.price}
        return {**table, **self.prices}

    @field_validator("prices", mode="before")
    @classmethod
    def _parse_prices(cls, value: Any) -> Any:
        """MODEL_PRICES="model=in/out,..." from env; a mapping from config.yaml."""
        if not isinstance(value, str):
            return value
        prices: dict[str, tuple[str, str]] = {}
        for item in _csv(value):
            model, eq, pair = item.partition("=")
            inp, slash, outp = pair.partition("/")
            if not (eq and slash and model.strip()):
                raise ValueError(f"cannot parse {item!r}, expected model=in/out")
            prices[model.strip()] = (inp, outp)
        return prices


# --- notify / bridge ------------------------------------------------------

class TelegramSection(_Section):
    enabled: bool = False
    token: str = ""  # also polled by the Review Bridge — nothing else may getUpdates
    chat_ids: list[str] = []
    # TELEGRAM_CHAT_ID[_N]; used only when chat_ids is empty
    legacy_chat_ids: list[str] = Field(default=[], exclude=True)
    # where tester reports go besides the bridge chat; empty = chat_ids
    tester_report_chat_ids: list[str] = []

    _split = field_validator("chat_ids", "tester_report_chat_ids", mode="before")(_csv)

    @model_validator(mode="after")
    def _legacy(self) -> TelegramSection:
        if not self.chat_ids:
            self.chat_ids = list(self.legacy_chat_ids)
        return self


class NotifySection(_Section):
    telegram: TelegramSection = TelegramSection()


class BridgeSection(_Section):
    enabled: bool = False
    chat_id: str = ""
    # measured AIManager latency (2026-07-24): 49s, 73s, 81s, ~180s. 90s dropped
    # answers that were still coming — the cost of waiting is latency, not money
    question_timeout: int = Field(default=240, gt=0)
    answer_grace: float = Field(default=6.0, ge=0)
    max_questions_per_mr: int = Field(default=10, ge=0)
    rate_per_hour: int = Field(default=25, ge=0)
    answer_bot_id: str = ""


# --- pipeline -------------------------------------------------------------

class StagesSection(_Section):
    investigator: bool = False
    tester_report: bool = False
    # the review stage gets read-only repo tools so it VERIFIES cross-file
    # concerns itself instead of asking the author to "confirm" them (dev
    # feedback 2026-07-31: prompt rules alone still let hedges through, because
    # a diff-only reviewer structurally cannot check anything outside the diff)
    review_repo_tools: bool = True
    # answer developer replies in MR discussion threads (needs note_events —
    # "Comments" — enabled on the project webhooks)
    dialogue: bool = True


class PipelineSection(_Section):
    language: str = "en"
    review_for_conflict: bool = False
    # replaces the main-tier REVIEW_SYSTEM prompt (English; per-team focus
    # belongs in the repo's .ai-review.md instead)
    review_prompt: str = ""
    stages: StagesSection = StagesSection()
    review_max_tool_calls: int = Field(default=8, ge=0)
    dialogue_max_replies_per_mr: int = Field(default=20, ge=0)
    investigator_max_iterations: int = Field(default=30, gt=0)

    @field_validator("language", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


# --- infrastructure -------------------------------------------------------

class RepoCacheSection(_Section):
    dir: str = "repos"
    max_gb: float = Field(default=30.0, gt=0)
    # clone -> investigate -> remove (for small disks); off = LRU cache within the budget
    ephemeral: bool = False


class DedupeSection(_Section):
    ttl: int = Field(default=600, ge=0)
    burst_seconds: int = Field(default=30, ge=0)


class StorageSection(_Section):
    # disposable response cache — swept by age. Durable state (overrides,
    # reviewed SHAs, model catalog) lives in state_dir: sharing one dir let the
    # 48h sweep delete model_overrides.json and reviewed_shas.json
    ai_cache_dir: str = "cache/ai"
    state_dir: str = "state"
    log_dir: str = "logs"


class ServerSection(_Section):
    workers: int = Field(default=2, ge=0)
    debug: bool = False
    # bearer token for GET /stats; empty = direct local access only
    stats_token: str = ""
    # HTTP Basic credentials for /stats & /dashboard (browser login prompt)
    stats_user: str = ""
    stats_password: str = ""


class NetworkSection(_Section):
    http_proxy: str = ""
    socks_proxy: str = ""  # host:port

    @property
    def proxy_url(self) -> str | None:
        """Proxy URL for httpx clients. HTTP proxy takes precedence (v1 behavior)."""
        if self.http_proxy:
            return self.http_proxy
        if self.socks_proxy:
            return f"socks5://{self.socks_proxy}"
        return None

    @property
    def git_proxy(self) -> str:
        """git's http.proxy value (socks5h: resolve hostnames through the proxy)."""
        if self.http_proxy:
            return self.http_proxy
        return f"socks5h://{self.socks_proxy}" if self.socks_proxy else ""

    @property
    def requests_proxies(self) -> dict | None:
        """Proxies dict for requests-based clients (python-gitlab)."""
        if self.http_proxy:
            return {"http": self.http_proxy, "https": self.http_proxy}
        if self.socks_proxy:
            url = f"socks5h://{self.socks_proxy}"
            return {"http": url, "https": url}
        return None


# --- env mapping ----------------------------------------------------------

# env var -> dotted settings path. Empty values count as unset (v1 compose
# passed `${VAR:-}` through as "", which meant "use the default").
ENV_FIELDS: dict[str, str] = {
    "AI_PROVIDER": "llm.provider",
    "ANTHROPIC_API_URL": "llm.anthropic.api_url",
    "ANTHROPIC_API_KEY": "llm.anthropic.api_key",
    "ANTHROPIC_API_KEY_GATEWAY": "llm.anthropic.gateway_key",
    "OPENROUTER_API_TOKEN": "llm.openrouter.token",
    "ANTHROPIC_FAST_MODEL": "llm.tiers.fast.model",
    "ANTHROPIC_MAIN_MODEL": "llm.tiers.main.model",
    "ANTHROPIC_SMART_MODEL": "llm.tiers.smart.model",
    "OPENROUTER_FALLBACK_FAST": "llm.tiers.fast.fallback",
    "OPENROUTER_FALLBACK_MAIN": "llm.tiers.main.fallback",
    "OPENROUTER_FALLBACK_SMART": "llm.tiers.smart.fallback",
    "MODEL_PRICES": "llm.prices",
    "AI_CACHE_TTL": "llm.cache_ttl",
    "AI_CACHE_ALERT_MIN_INPUT": "llm.cache_alert_min_input",
    "AI_TIMEOUT": "llm.timeout",
    "AI_AGENT_TIMEOUT": "llm.agent_timeout",
    "AI_RATE_LIMIT": "llm.rate_limit",
    "AI_DEBUG": "llm.debug",
    "AI_MAX_INPUT_TOKENS": "llm.max_input_tokens",
    "TELEGRAM": "notify.telegram.enabled",
    "TELEGRAM_BOT_TOKEN": "notify.telegram.token",
    "TELEGRAM_CHAT_IDS": "notify.telegram.chat_ids",
    "TESTER_REPORT_CHAT_IDS": "notify.telegram.tester_report_chat_ids",
    "BRIDGE": "bridge.enabled",
    "REVIEW_BRIDGE_CHAT_ID": "bridge.chat_id",
    "BRIDGE_QUESTION_TIMEOUT": "bridge.question_timeout",
    "BRIDGE_ANSWER_GRACE": "bridge.answer_grace",
    "BRIDGE_MAX_QUESTIONS_PER_MR": "bridge.max_questions_per_mr",
    "BRIDGE_RATE_PER_HOUR": "bridge.rate_per_hour",
    "BRIDGE_ANSWER_BOT_ID": "bridge.answer_bot_id",
    "REVIEW_LANGUAGE": "pipeline.language",
    "REVIEW_FOR_CONFLICT": "pipeline.review_for_conflict",
    "REVIEW_PROMPT": "pipeline.review_prompt",
    "INVESTIGATOR": "pipeline.stages.investigator",
    "TESTER_REPORT": "pipeline.stages.tester_report",
    "REVIEW_REPO_TOOLS": "pipeline.stages.review_repo_tools",
    "MR_DIALOGUE": "pipeline.stages.dialogue",
    "REVIEW_MAX_TOOL_CALLS": "pipeline.review_max_tool_calls",
    "DIALOGUE_MAX_REPLIES_PER_MR": "pipeline.dialogue_max_replies_per_mr",
    "INVESTIGATOR_MAX_ITERATIONS": "pipeline.investigator_max_iterations",
    "REPO_CACHE_DIR": "repo_cache.dir",
    "REPO_CACHE_MAX_GB": "repo_cache.max_gb",
    "REPO_CACHE_EPHEMERAL": "repo_cache.ephemeral",
    "DEDUPE_TTL": "dedupe.ttl",
    "DEDUPE_BURST_SECONDS": "dedupe.burst_seconds",
    "AI_CACHE_DIR": "storage.ai_cache_dir",
    "STATE_DIR": "storage.state_dir",
    "AI_LOG_DIR": "storage.log_dir",
    "AI_WORKERS": "server.workers",
    "DEBUG": "server.debug",
    "STATS_TOKEN": "server.stats_token",
    "STATS_USER": "server.stats_user",
    "STATS_PASSWORD": "server.stats_password",
    "HTTP_PROXY": "network.http_proxy",
    "SOCKS_PROXY": "network.socks_proxy",
}
_PATH_TO_ENV = {path: env for env, path in ENV_FIELDS.items()}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _put(tree: dict, path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for key in parents:
        tree = tree.setdefault(key, {})
    tree[leaf] = value


def _legacy_instances() -> list[dict]:
    out = []
    for slot in LEGACY_INSTANCE_SLOTS:
        trio = {"url": _env(f"GITLAB_URL{slot}"), "token": _env(f"GITLAB_TOKEN{slot}"),
                "webhook_token": _env(f"XGITLABTOKEN{slot}")}
        # keyed on the URL: GITLAB_TOKEN[_N] alone may just hold a secret that
        # config.yaml references; a URL without a token is a validation error
        if trio["url"]:
            out.append({"name": f"instance{slot}" if slot else "primary", **trio})
    return out


class FlatEnvSource(PydanticBaseSettingsSource):
    """The flat v1 env names (ENV_FIELDS) plus the legacy numbered lists."""

    def get_field_value(self, field, field_name):  # unused: __call__ builds the tree
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        tree: dict[str, Any] = {}
        for env, path in ENV_FIELDS.items():
            if _env(env):
                _put(tree, path, _env(env))
        if instances := _legacy_instances():
            _put(tree, "gitlab.legacy_instances", instances)
        if chats := [_env(f"TELEGRAM_CHAT_ID{s}") for s in LEGACY_CHAT_SLOTS
                     if _env(f"TELEGRAM_CHAT_ID{s}")]:
            _put(tree, "notify.telegram.legacy_chat_ids", chats)
        return tree


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(node: Any) -> Any:
    """${NAME} / ${NAME:-default} in config.yaml strings -> env values."""
    if isinstance(node, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), node)
    if isinstance(node, list):
        return [_expand(item) for item in node]
    if isinstance(node, dict):
        return {key: _expand(value) for key, value in node.items()}
    return node


def config_file_path() -> Path:
    return Path(_env("CONFIG_FILE") or "config.yaml")


class YamlFileSource(PydanticBaseSettingsSource):
    def get_field_value(self, field, field_name):
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = config_file_path()
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        return _expand(data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="forbid")

    gitlab: GitLabSection = GitLabSection()
    llm: LLMSection = LLMSection()
    notify: NotifySection = NotifySection()
    bridge: BridgeSection = BridgeSection()
    pipeline: PipelineSection = PipelineSection()
    repo_cache: RepoCacheSection = RepoCacheSection()
    dedupe: DedupeSection = DedupeSection()
    storage: StorageSection = StorageSection()
    server: ServerSection = ServerSection()
    network: NetworkSection = NetworkSection()

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        # the stock env source is left out on purpose: it would read e.g.
        # BRIDGE=on as the whole `bridge` section and fail on it
        return init_settings, FlatEnvSource(settings_cls), YamlFileSource(settings_cls)

    def model_for_tier(self, tier: Tier | str) -> str:
        return self.llm.tiers.get(tier).model

    def fallback_chain(self, tier: Tier | str) -> list[str]:
        return self.llm.tiers.get(tier).fallback


def describe_errors(exc: ValidationError) -> str:
    """One line per bad value, naming the env var that sets it when there is one."""
    lines = []
    for err in exc.errors():
        path = ".".join(str(part) for part in err["loc"])
        env = _PATH_TO_ENV.get(path)
        where = f"{path} (env {env})" if env else path
        if path.startswith("gitlab"):
            where += " — GitLab instances: config.yaml gitlab.instances or GITLAB_URL/TOKEN/XGITLABTOKEN[_N]"
        got = f", got {err['input']!r}" if not isinstance(err.get("input"), dict) else ""
        lines.append(f"  {where}: {err['msg']}{got}")
    return "\n".join(lines)


def load_settings() -> Settings:
    """Settings() or a readable SystemExit — a misconfigured service must not
    start on silently substituted defaults."""
    try:
        return Settings()
    except ValidationError as exc:
        raise SystemExit("Configuration error:\n" + describe_errors(exc)) from None
    except (OSError, ValueError, yaml.YAMLError) as exc:  # config.yaml unreadable
        raise SystemExit(f"Configuration error: {config_file_path()}: {exc}") from None


# env vars that no longer do anything -> name of the successor (or "" if none).
# Set in a .env they are ignored silently, so startup logs a WARNING per name.
RETIRED_ENV_VARS = {
    "PIPELINE_V2": "",  # the tiered pipeline is the only path now
    "GEMINI_API_KEY": "",
    "GEMINI_CACHE_TTL": "AI_CACHE_TTL",
    "GEMINI_CACHE_DIR": "AI_CACHE_DIR",
    "GEMINI_TIMEOUT": "AI_TIMEOUT",
    "GEMINI_RATE_LIMIT": "AI_RATE_LIMIT",
    "GEMINI_DEBUG": "AI_DEBUG",
    "GEMINI_LOG_DIR": "AI_LOG_DIR",
    "GEMINI_PROMPT": "REVIEW_PROMPT",
    "GEMINI_PROMPT_RU": "",  # parity mode only
}


def retired_env_vars_in_use() -> list[str]:
    """One message per retired variable still present in the environment."""
    return [f"{name} is no longer read" + (f" — rename it to {new}" if new else "")
            for name, new in RETIRED_ENV_VARS.items() if os.getenv(name)]


def deprecated_env_vars_in_use(cfg: Settings) -> list[str]:
    """Legacy numbered lists: still honoured (unless config.yaml / the new
    variable already defines the list), removed one release after stage 8."""
    messages = []
    gitlab_vars = [f"GITLAB_URL{s}" for s in LEGACY_INSTANCE_SLOTS if _env(f"GITLAB_URL{s}")]
    if gitlab_vars:
        used = cfg.gitlab.instances == cfg.gitlab.legacy_instances
        messages.append(
            f"{', '.join(gitlab_vars)}: deprecated — move the instances to config.yaml "
            "gitlab.instances (token: ${GITLAB_TOKEN} keeps the secret in env, see "
            "config.example.yaml)"
            + ("" if used else "; IGNORED, config.yaml defines gitlab.instances"))
    chat_vars = [f"TELEGRAM_CHAT_ID{s}" for s in LEGACY_CHAT_SLOTS if _env(f"TELEGRAM_CHAT_ID{s}")]
    if chat_vars:
        used = cfg.notify.telegram.chat_ids == cfg.notify.telegram.legacy_chat_ids
        messages.append(
            f"{', '.join(chat_vars)}: deprecated — use TELEGRAM_CHAT_IDS=a,b or config.yaml "
            "notify.telegram.chat_ids" + ("" if used else "; IGNORED, chat_ids is set"))
    return messages


_SECRET = re.compile(r"token|key|password", re.IGNORECASE)


def masked_dump(cfg: Settings) -> dict:
    """Effective config with secrets masked — for `python -m reviewer.config`."""
    def mask(node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            return {k: mask(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [mask(v, key) for v in node]
        if isinstance(node, str) and node and _SECRET.search(key):
            return node[:4] + "…"
        return node
    return mask(cfg.model_dump())


if __name__ == "__main__":  # pre-deploy check: prints the effective config or the errors
    from reviewer.bootstrap import main_print_config

    main_print_config()
