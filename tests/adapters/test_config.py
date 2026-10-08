"""Typed config (pydantic-settings): env names, config.yaml, validation, legacy aliases."""

from __future__ import annotations

import pytest

from reviewer.config import Settings
from reviewer.domain.models import InstanceRef
from tests.factories import (
    make_services,
    make_settings,
)


def test_proxy_precedence(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://p:1")
    monkeypatch.setenv("SOCKS_PROXY", "s:2")
    cfg = Settings()
    assert cfg.network.proxy_url == "http://p:1"
    assert cfg.network.requests_proxies == {"http": "http://p:1", "https": "http://p:1"}
    monkeypatch.delenv("HTTP_PROXY")
    cfg = Settings()
    assert cfg.network.proxy_url == "socks5://s:2"
    assert cfg.network.requests_proxies["https"] == "socks5h://s:2"


def test_instances_routing_keyed_by_webhook_token(clean_env, monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "https://a")
    monkeypatch.setenv("GITLAB_TOKEN", "t1")
    monkeypatch.setenv("XGITLABTOKEN", "hook1")
    monkeypatch.setenv("GITLAB_URL_2", "https://b")
    monkeypatch.setenv("GITLAB_TOKEN_2", "t2")
    monkeypatch.setenv("XGITLABTOKEN_2", "hook2")
    cfg = Settings()
    assert cfg.gitlab.routes["hook1"].name == "primary"
    assert cfg.gitlab.routes["hook2"].url == "https://b"
    # stage 8: the numbered env trios still work, with a deprecation warning
    from reviewer.config import deprecated_env_vars_in_use
    [message] = deprecated_env_vars_in_use(cfg)
    assert "GITLAB_URL_2" in message and "config.yaml" in message and "IGNORED" not in message


def test_tier_mapping_and_fallback_chains():
    cfg = Settings()
    assert cfg.model_for_tier("fast") == cfg.llm.tiers.fast.model
    assert cfg.fallback_chain("smart")[0].startswith("anthropic/")


def test_float_env_empty_string(monkeypatch):
    monkeypatch.setenv("AI_RATE_LIMIT", "")
    monkeypatch.setenv("REPO_CACHE_MAX_GB", "")
    cfg = Settings()
    assert cfg.llm.rate_limit == 2.0
    assert cfg.repo_cache.max_gb == 30.0


def test_pyproject_declares_runtime_deps():
    import tomllib

    with open("pyproject.toml", "rb") as fh:
        deps = " ".join(tomllib.load(fh)["project"]["dependencies"])
    for dep in ("anthropic", "httpx[socks]", "fastapi", "python-gitlab", "uvicorn",
                "requests"):
        assert dep in deps, f"{dep} missing from pyproject.toml"


def test_dialogue_and_review_tools_flags(monkeypatch):
    for var in ("REVIEW_REPO_TOOLS", "MR_DIALOGUE", "REVIEW_MAX_TOOL_CALLS",
                "DIALOGUE_MAX_REPLIES_PER_MR"):
        monkeypatch.delenv(var, raising=False)
    cfg = Settings()
    assert cfg.pipeline.stages.review_repo_tools is True and cfg.pipeline.stages.dialogue is True
    assert cfg.pipeline.review_max_tool_calls == 8
    assert cfg.pipeline.dialogue_max_replies_per_mr == 20
    monkeypatch.setenv("REVIEW_REPO_TOOLS", "off")
    monkeypatch.setenv("MR_DIALOGUE", "off")
    cfg = Settings()
    assert cfg.pipeline.stages.review_repo_tools is False and cfg.pipeline.stages.dialogue is False


def test_retired_gemini_aliases_are_no_longer_read(monkeypatch):
    for name in ("AI_TIMEOUT", "AI_DEBUG", "AI_CACHE_DIR", "REVIEW_PROMPT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_TIMEOUT", "5")
    monkeypatch.setenv("GEMINI_DEBUG", "true")
    monkeypatch.setenv("GEMINI_CACHE_DIR", "/tmp/old-cache")
    monkeypatch.setenv("GEMINI_PROMPT", "old checklist")
    cfg = Settings()
    assert cfg.llm.timeout == 300
    assert cfg.llm.debug is False
    assert cfg.storage.ai_cache_dir == "cache/ai"
    assert cfg.pipeline.review_prompt == ""
    assert not hasattr(cfg, "pipeline_v2")


def test_review_prompt_override_has_a_non_legacy_name(monkeypatch):
    monkeypatch.setenv("REVIEW_PROMPT", "custom checklist")
    assert Settings().pipeline.review_prompt == "custom checklist"


def test_retired_env_vars_are_reported_with_successor(monkeypatch):
    from reviewer.config import RETIRED_ENV_VARS, retired_env_vars_in_use
    for name in RETIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert retired_env_vars_in_use() == []
    monkeypatch.setenv("PIPELINE_V2", "on")
    monkeypatch.setenv("GEMINI_PROMPT", "x")
    assert retired_env_vars_in_use() == [
        "PIPELINE_V2 is no longer read",
        "GEMINI_PROMPT is no longer read — rename it to REVIEW_PROMPT"]


def test_startup_warns_about_retired_and_deprecated_vars(monkeypatch, caplog, tmp_path):
    from fastapi.testclient import TestClient

    from reviewer import server
    # lifespan runs state_layout.migrate: keep it off the developer's cache/
    cfg = make_settings(tmp_path, gitlab__routes={}, bridge__enabled=False)
    monkeypatch.setenv("PIPELINE_V2", "off")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_3", "-100x")
    app = server.create_app(make_services(cfg))
    with caplog.at_level("WARNING", logger="reviewer.bootstrap"), TestClient(app) as client:
        flags = client.get("/").json()["flags"]
    assert "pipeline_v2" not in flags
    assert "PIPELINE_V2 is no longer read" in caplog.text
    assert "TELEGRAM_CHAT_ID_3" in caplog.text and "deprecated" in caplog.text


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No config env vars at all (the developer's .env is already in os.environ)."""
    from reviewer import config
    legacy = [f"{p}{s}" for s in config.LEGACY_INSTANCE_SLOTS
              for p in ("GITLAB_URL", "GITLAB_TOKEN", "XGITLABTOKEN")]
    legacy += [f"TELEGRAM_CHAT_ID{s}" for s in config.LEGACY_CHAT_SLOTS]
    for name in [*config.ENV_FIELDS, *legacy]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "config.yaml"))
    return tmp_path / "config.yaml"


def test_config_bad_values_stop_startup_naming_the_env_var(clean_env, monkeypatch):
    # #9: v1 silently replaced AI_TIMEOUT=abc with 300 and INVESTIGATOR=maybe with off
    from reviewer.config import load_settings
    monkeypatch.setenv("AI_TIMEOUT", "abc")
    monkeypatch.setenv("INVESTIGATOR", "maybe")
    monkeypatch.setenv("AI_PROVIDER", "gemini")  # retired in stage 6 -> error, not "runs as anthropic"
    with pytest.raises(SystemExit) as exc:
        load_settings()
    text = str(exc.value)
    assert "llm.timeout (env AI_TIMEOUT)" in text and "'abc'" in text
    assert "env INVESTIGATOR" in text and "'maybe'" in text
    assert "env AI_PROVIDER" in text


def test_config_instance_without_token_is_an_error(clean_env, monkeypatch):
    # v1 skipped a half-configured instance silently: its webhooks then hit
    # "unknown token" with nothing in the startup log saying why
    from reviewer.config import load_settings
    monkeypatch.setenv("GITLAB_URL_2", "https://b")
    monkeypatch.setenv("XGITLABTOKEN_2", "hook2")
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert "gitlab.legacy_instances.0.token" in str(exc.value)


def test_config_empty_env_values_mean_default(clean_env, monkeypatch):
    # v1 compose passed ${VAR:-} through as "" — that must keep meaning "default"
    monkeypatch.setenv("AI_WORKERS", "")
    monkeypatch.setenv("ANTHROPIC_SMART_MODEL", "")
    monkeypatch.setenv("GITLAB_URL_2", "")
    cfg = Settings()
    assert cfg.server.workers == 2
    assert cfg.llm.tiers.smart.model == "claude-opus-5"
    assert cfg.gitlab.instances == []


def test_config_legacy_telegram_chats_and_new_csv(clean_env, monkeypatch):
    from reviewer.config import deprecated_env_vars_in_use
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_2", "-2")
    cfg = Settings()
    assert cfg.notify.telegram.chat_ids == ["-1", "-2"]
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "-7, -8")
    cfg = Settings()
    assert cfg.notify.telegram.chat_ids == ["-7", "-8"]
    assert "IGNORED" in deprecated_env_vars_in_use(cfg)[0]


YAML_CONFIG = """
gitlab:
  instances:
    - name: primary
      url: https://lab.example
      token: ${LAB_TOKEN}
      webhook_token: ${LAB_HOOK:-fallback-hook}
llm:
  tiers:
    smart: {model: openai/gpt-5.6-terra}
  prices:
    z-ai/glm-5: [0.6, 2.2]
notify:
  telegram:
    chat_ids: ["-100a"]
"""


def test_config_yaml_instances_tiers_prices(clean_env, monkeypatch):
    from reviewer import usage
    from reviewer.config import deprecated_env_vars_in_use
    clean_env.write_text(YAML_CONFIG, encoding="utf-8")
    monkeypatch.setenv("LAB_TOKEN", "glpat-secret")
    monkeypatch.setenv("GITLAB_URL", "https://legacy")  # yaml wins, legacy is reported
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    monkeypatch.setenv("XGITLABTOKEN", "h")
    monkeypatch.setenv("ANTHROPIC_MAIN_MODEL", "claude-sonnet-5-5")  # env beats yaml/defaults
    cfg = Settings()
    assert cfg.gitlab.routes == {"fallback-hook": InstanceRef(
        "primary", "https://lab.example", "glpat-secret")}
    assert "IGNORED" in deprecated_env_vars_in_use(cfg)[0]
    assert cfg.model_for_tier("smart") == "openai/gpt-5.6-terra"
    assert cfg.fallback_chain("smart")[0] == "anthropic/claude-opus-5"  # default chain kept
    assert cfg.model_for_tier("main") == "claude-sonnet-5-5"
    assert cfg.llm.prices == {"z-ai/glm-5": (0.6, 2.2)}
    assert cfg.notify.telegram.chat_ids == ["-100a"]
    assert usage.Pricing(cfg.llm.prices).price_of("z-ai/glm-5") == (0.6, 2.2)


def test_config_yaml_unknown_tier_or_key_is_an_error(clean_env):
    from reviewer.config import load_settings
    clean_env.write_text("llm:\n  tiers:\n    huge: {model: x}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="llm.tiers.huge"):
        load_settings()
    clean_env.write_text("pipeline:\n  stages:\n    investigatr: true\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="investigatr"):
        load_settings()
    clean_env.write_text("llm: [unclosed\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="config.yaml"):
        load_settings()
    clean_env.unlink()
    with pytest.raises(ValueError, match="unknown tier"):
        Settings().model_for_tier("huge")


def test_config_model_prices_env(clean_env, monkeypatch):
    from reviewer.config import load_settings
    monkeypatch.setenv("MODEL_PRICES", "claude-sonnet-5=3/15, x/y=0.5/1")
    assert Settings().llm.prices == {"claude-sonnet-5": (3.0, 15.0), "x/y": (0.5, 1.0)}
    monkeypatch.setenv("MODEL_PRICES", "claude-sonnet-5=3")  # v1 logged and skipped it
    with pytest.raises(SystemExit, match="MODEL_PRICES"):
        load_settings()


def test_config_prod_env_gives_the_v1_configuration(clean_env, monkeypatch):
    # 8.6: the production .env shape (secrets replaced) yields the same values
    # the v1 dataclass produced — expected values written out from the old
    # _bool/_int/_csv rules, not from the new code
    prod = {
        "GITLAB_URL": "https://lab.smysl.pro", "GITLAB_TOKEN": "glpat-1", "XGITLABTOKEN": "hook-1",
        "GITLAB_URL_2": "https://lab.catzwolf.ru", "GITLAB_TOKEN_2": "glpat-2",
        "XGITLABTOKEN_2": "hook-2",
        "AI_PROVIDER": "anthropic", "ANTHROPIC_API_URL": "https://gw/anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_API_KEY_GATEWAY": "cfut_x",
        "ANTHROPIC_FAST_MODEL": "claude-haiku-4-5", "ANTHROPIC_MAIN_MODEL": "claude-sonnet-5",
        "ANTHROPIC_SMART_MODEL": "claude-opus-5", "OPENROUTER_API_TOKEN": "sk-or-x",
        "INVESTIGATOR": "on", "BRIDGE": "on", "TESTER_REPORT": "on",
        "REVIEW_REPO_TOOLS": "on", "MR_DIALOGUE": "on", "REVIEW_BRIDGE_CHAT_ID": "-100bridge",
        "AI_DEBUG": "false", "REPO_CACHE_MAX_GB": "25",
        "TELEGRAM": "on", "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100team",
        "REVIEW_LANGUAGE": "ru", "REVIEW_FOR_CONFLICT": "false", "DEBUG": "false",
    }
    for name, value in prod.items():
        monkeypatch.setenv(name, value)
    cfg = Settings()
    assert cfg.gitlab.routes == {
        "hook-1": InstanceRef("primary", "https://lab.smysl.pro", "glpat-1"),
        "hook-2": InstanceRef("instance_2", "https://lab.catzwolf.ru", "glpat-2"),
    }
    assert (cfg.llm.provider, cfg.llm.anthropic.api_url, cfg.llm.anthropic.api_key,
            cfg.llm.anthropic.gateway_key, cfg.llm.openrouter.token) == (
        "anthropic", "https://gw/anthropic", "sk-ant-x", "cfut_x", "sk-or-x")
    assert [cfg.model_for_tier(t) for t in ("fast", "main", "smart")] == [
        "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"]
    assert cfg.fallback_chain("main") == [
        "anthropic/claude-sonnet-5", "google/gemini-3.6-flash", "deepseek/deepseek-v4-pro"]
    stages = cfg.pipeline.stages
    assert (stages.investigator, cfg.bridge.enabled, stages.tester_report,
            stages.review_repo_tools, stages.dialogue) == (True, True, True, True, True)
    assert (cfg.llm.cache_ttl, cfg.llm.timeout, cfg.llm.agent_timeout, cfg.llm.rate_limit,
            cfg.llm.max_input_tokens, cfg.llm.cache_alert_min_input, cfg.llm.debug) == (
        3600, 300, 600, 2.0, 300_000, 100_000, False)
    assert (cfg.storage.ai_cache_dir, cfg.storage.state_dir, cfg.storage.log_dir) == (
        "cache/ai", "state", "logs")
    assert (cfg.server.workers, cfg.server.debug, cfg.dedupe.ttl, cfg.dedupe.burst_seconds) == (
        2, False, 600, 30)
    assert (cfg.repo_cache.dir, cfg.repo_cache.max_gb, cfg.repo_cache.ephemeral) == (
        "repos", 25.0, False)
    assert (cfg.pipeline.language, cfg.pipeline.review_for_conflict, cfg.pipeline.review_prompt,
            cfg.pipeline.review_max_tool_calls, cfg.pipeline.dialogue_max_replies_per_mr,
            cfg.pipeline.investigator_max_iterations) == ("ru", False, "", 8, 20, 30)
    assert (cfg.notify.telegram.enabled, cfg.notify.telegram.token,
            cfg.notify.telegram.chat_ids, cfg.notify.telegram.tester_report_chat_ids) == (
        True, "123:abc", ["-100team"], [])
    assert (cfg.bridge.chat_id, cfg.bridge.question_timeout, cfg.bridge.answer_grace,
            cfg.bridge.max_questions_per_mr, cfg.bridge.rate_per_hour,
            cfg.bridge.answer_bot_id) == ("-100bridge", 240, 6.0, 10, 25, "")
    assert cfg.network.proxy_url is None and cfg.llm.prices == {}


def test_config_masked_dump_hides_secrets(clean_env, monkeypatch):
    from reviewer.config import masked_dump
    monkeypatch.setenv("GITLAB_URL", "https://a")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-very-secret")
    monkeypatch.setenv("XGITLABTOKEN", "hook-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    dump = str(masked_dump(Settings()))
    assert "very-secret" not in dump and "hook-secret" not in dump
    assert "https://a" in dump and "max_input_tokens" in dump


def test_config_yaml_referencing_gitlab_token_env_is_not_deprecated(clean_env, monkeypatch):
    # config.example.yaml keeps secrets in the familiar GITLAB_TOKEN/XGITLABTOKEN
    # vars; only GITLAB_URL[_N] marks a legacy env-defined instance
    from reviewer.config import deprecated_env_vars_in_use
    clean_env.write_text(
        "gitlab:\n  instances:\n    - {name: primary, url: https://a, "
        "token: '${GITLAB_TOKEN}', webhook_token: '${XGITLABTOKEN}'}\n", encoding="utf-8")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-1")
    monkeypatch.setenv("XGITLABTOKEN", "hook-1")
    cfg = Settings()
    assert cfg.gitlab.routes["hook-1"].token == "glpat-1"
    assert deprecated_env_vars_in_use(cfg) == []
