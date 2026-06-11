"""Offline unit tests for v2 (no network, no API keys needed).

Run: .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import time

import pytest

from reviewer import ai_client as ai_mod
from reviewer import bridge as bridge_mod
from reviewer import gitlab_io
from reviewer.ai_client import AIClient, extract_json
from reviewer.config import Settings
from reviewer.repo_cache import _safe_path, repo_grep, repo_list_tree, repo_read_file
from reviewer.server import ReviewQueue


# --- config ---

def test_proxy_precedence(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://p:1")
    monkeypatch.setenv("SOCKS_PROXY", "s:2")
    cfg = Settings()
    assert cfg.proxy_url == "http://p:1"
    assert cfg.requests_proxies == {"http": "http://p:1", "https": "http://p:1"}
    monkeypatch.delenv("HTTP_PROXY")
    cfg = Settings()
    assert cfg.proxy_url == "socks5://s:2"
    assert cfg.requests_proxies["https"] == "socks5h://s:2"


def test_instances_routing_keyed_by_webhook_token(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("GITLAB_", "XGITLABTOKEN")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITLAB_URL", "https://a")
    monkeypatch.setenv("GITLAB_TOKEN", "t1")
    monkeypatch.setenv("XGITLABTOKEN", "hook1")
    monkeypatch.setenv("GITLAB_URL_2", "https://b")
    monkeypatch.setenv("GITLAB_TOKEN_2", "t2")
    monkeypatch.setenv("XGITLABTOKEN_2", "hook2")
    cfg = Settings()
    assert cfg.gitlab_instances["hook1"]["name"] == "primary"
    assert cfg.gitlab_instances["hook2"]["url"] == "https://b"


def test_tier_mapping_and_fallback_chains():
    cfg = Settings()
    assert cfg.model_for_tier("fast") == cfg.model_fast
    assert cfg.fallback_chain("smart")[0].startswith("anthropic/")


# --- ai_client helpers ---

def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('prose\n```json\n{"a": 1}\n```\nmore') == {"a": 1}
    assert extract_json('text {"a": {"b": 2}} tail') == {"a": {"b": 2}}
    assert extract_json("no json here") is None


def test_cache_roundtrip(tmp_path):
    cfg = Settings()
    cfg.ai_cache_dir = str(tmp_path)
    cfg.ai_cache_ttl = 3600
    client = AIClient(cfg)
    key = client._cache_key("m", "sys", "user")
    assert client._cache_get(key) is None
    client._cache_put(key, "result text")
    assert client._cache_get(key) == "result text"
    # expired entries are misses
    old = tmp_path / key
    import os
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert client._cache_get(key) is None


def test_gateway_auth_modes():
    # mode 1: real key + cfut token -> both x-api-key and cf-aig-authorization
    cfg = Settings()
    cfg.anthropic_api_url = "https://gateway.example/anthropic"
    cfg.anthropic_api_key = "sk-ant-real"
    cfg.anthropic_gateway_key = "cfut_abc123"
    primary = AIClient(cfg).primary
    assert primary.api_key == "sk-ant-real"
    assert primary.default_headers.get("cf-aig-authorization") == "Bearer cfut_abc123"

    # mode 2: cfut only (Unified Billing) -> header + dummy x-api-key
    cfg2 = Settings()
    cfg2.anthropic_api_url = "https://gateway.example/anthropic"
    cfg2.anthropic_api_key = ""
    cfg2.anthropic_gateway_key = "cfut_abc123"
    primary2 = AIClient(cfg2).primary
    assert primary2.api_key == "gateway"
    assert primary2.default_headers.get("cf-aig-authorization") == "Bearer cfut_abc123"

    # mode 3 / legacy: real key stored in the GATEWAY var -> plain x-api-key
    cfg3 = Settings()
    cfg3.anthropic_api_url = "https://gateway.example/anthropic"
    cfg3.anthropic_api_key = ""
    cfg3.anthropic_gateway_key = "sk-ant-real"
    primary3 = AIClient(cfg3).primary
    assert primary3.api_key == "sk-ant-real"
    assert primary3.default_headers.get("cf-aig-authorization") is None


def test_primary_params_per_tier():
    client = AIClient(Settings())
    assert client._primary_params("fast", None) == {}        # Haiku: no thinking/effort
    smart = client._primary_params("smart", "high")
    assert smart["thinking"] == {"type": "adaptive"}
    assert smart["output_config"] == {"effort": "high"}
    main = client._primary_params("main", None)
    assert "output_config" not in main


def test_input_size_guard():
    cfg = Settings()
    cfg.ai_max_input_tokens = 10
    client = AIClient(cfg)
    with pytest.raises(ai_mod.AIInputTooLargeError):
        client.guard_input_size("x" * 1000)


# --- bridge ---

def test_strip_usage_footer():
    text = "Issue PBV-123 is about cart totals.\nAcceptance: totals match.\nsonnet: 12.3k tok $0.04"
    assert bridge_mod.strip_usage_footer(text).endswith("totals match.")
    # multi-line answers without a footer are untouched
    clean = "line one\nline two"
    assert bridge_mod.strip_usage_footer(clean) == clean
    # a colon line WITHOUT digits is content, not a footer
    keep = "Steps:\nDo the thing:"
    assert bridge_mod.strip_usage_footer(keep) == keep


def test_rate_window():
    window = bridge_mod._RateWindow(2)
    assert window.allow() and window.allow()
    assert not window.allow()


# --- repo tools sandbox ---

def test_repo_tools(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def handle_cart():\n    return 42\n")
    (tmp_path / "README.md").write_text("hello")

    hits = repo_grep(tmp_path, r"handle_cart")
    assert "src/app.py:1" in hits

    content = repo_read_file(tmp_path, "src/app.py")
    assert "1: def handle_cart():" in content

    tree = repo_list_tree(tmp_path)
    assert "src/" in tree and "README.md" in tree

    with pytest.raises(ValueError):
        _safe_path(tmp_path, "../../etc/passwd")
    assert "Error" in repo_read_file(tmp_path, "../secret")


# --- gitlab_io ---

WEBHOOK_PAYLOAD = {
    "object_attributes": {
        "action": "open", "iid": 7, "id": 100,
        "source_branch": "PBV-123-fix-cart", "target_branch": "master",
        "title": "Fix cart", "description": "closes ABC-9",
        "url": "https://lab/x/-/mergerequests/7",
        "last_commit": {"id": "deadbeef"},
    },
    "project": {"id": 1, "path_with_namespace": "g/x", "name": "x"},
    "user": {"username": "max", "name": "Max"},
}


def test_parse_webhook_url_fix_and_actions():
    parsed = gitlab_io.parse_merge_request_webhook(WEBHOOK_PAYLOAD)
    assert parsed is not None
    assert parsed["url"] == "https://lab/x/-/merge_requests/7"  # contractual URL typo fix
    assert parsed["last_commit"] == "deadbeef"

    closed = {**WEBHOOK_PAYLOAD,
              "object_attributes": {**WEBHOOK_PAYLOAD["object_attributes"], "action": "close"}}
    assert gitlab_io.parse_merge_request_webhook(closed) is None


def test_extract_jira_keys():
    parsed = gitlab_io.parse_merge_request_webhook(WEBHOOK_PAYLOAD)
    keys = gitlab_io.extract_jira_keys(parsed)
    assert keys == ["PBV-123", "ABC-9"]


def test_format_review_comment_language(monkeypatch):
    from reviewer.config import settings
    monkeypatch.setattr(settings, "review_language", "ru")
    comment = gitlab_io.format_review_comment("текст обзора")
    assert "Автоматический обзор кода" in comment
    monkeypatch.setattr(settings, "review_language", "en")
    assert "Automated Code Review" in gitlab_io.format_review_comment("review")


# --- review-fix regressions ---

def test_should_fallback_gating():
    import httpx as _httpx

    def status_error(code):
        request = _httpx.Request("POST", "https://x")
        response = _httpx.Response(code, request=request)
        cls = {429: __import__("anthropic").RateLimitError}.get(code)
        if cls:
            return cls("e", response=response, body=None)
        import anthropic as _a
        return _a.APIStatusError("e", response=response, body=None)

    assert ai_mod._should_fallback(status_error(429)) is True
    assert ai_mod._should_fallback(status_error(500)) is True
    assert ai_mod._should_fallback(status_error(529)) is True
    assert ai_mod._should_fallback(status_error(400)) is False  # our bug — surface it
    assert ai_mod._should_fallback(status_error(401)) is False
    assert ai_mod._should_fallback(ValueError("x")) is False


def test_strip_thinking_blocks():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "a"},
            {"type": "tool_use", "id": "1", "name": "t", "input": {}},
        ]},
    ]
    cleaned = ai_mod._strip_thinking(messages)
    types = [b["type"] for b in cleaned[1]["content"]]
    assert types == ["text", "tool_use"]
    assert messages[1]["content"][0]["type"] == "thinking"  # original untouched


def test_safe_path_rejects_sibling_prefix(tmp_path):
    # /x/repo must not authorize /x/repo-evil (startswith-prefix traversal)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    sibling = tmp_path / "repo-evil"
    sibling.mkdir()
    (sibling / "secret").write_text("s")
    with pytest.raises(ValueError):
        _safe_path(worktree, "../repo-evil/secret")


def test_repo_tools_skip_symlinks(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("LEAKED_TOKEN=abc")
    (worktree / "link.txt").symlink_to(secret)
    (worktree / "ok.py").write_text("LEAKED_TOKEN nope, just code")
    hits = repo_grep(worktree, "LEAKED_TOKEN")
    assert "link.txt" not in hits and "ok.py" in hits
    # reading through the symlink must fail (resolve+sandbox check or symlink check)
    assert "LEAKED" not in repo_read_file(worktree, "link.txt")
    assert "link.txt" not in repo_list_tree(worktree)


def test_repo_read_file_edges(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\n")
    assert "beyond end of file" in repo_read_file(tmp_path, "f.txt", start_line=10)
    assert "before start_line" in repo_read_file(tmp_path, "f.txt", start_line=2, end_line=1)


def test_float_env_empty_string(monkeypatch):
    monkeypatch.setenv("AI_RATE_LIMIT", "")
    monkeypatch.setenv("REPO_CACHE_MAX_GB", "")
    cfg = Settings()
    assert cfg.ai_rate_limit == 2.0
    assert cfg.repo_cache_max_gb == 30.0


def test_redact_credentials_in_git_errors():
    from reviewer.repo_cache import _redact
    msg = "fatal: unable to access 'https://oauth2:glpat-SECRET@lab.x/p.git/'"
    assert "glpat-SECRET" not in _redact(msg)
    assert "https://***@lab.x" in _redact(msg)


def test_requirements_declare_runtime_deps():
    reqs = open("requirements.txt").read()
    for dep in ("anthropic", "httpx[socks]"):
        assert dep in reqs, f"{dep} missing from requirements.txt"


def test_release_modes_keep_vs_ephemeral(tmp_path, monkeypatch):
    """release() keeps the bare repo by default; REPO_CACHE_EPHEMERAL drops it."""
    import shutil as _shutil
    import subprocess as _sp
    if _shutil.which("git") is None:
        pytest.skip("git not available")
    from reviewer import repo_cache as rc

    def git(*args):
        _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                check=True, capture_output=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", str(origin))
    (origin / "f.txt").write_text("x")
    git("-C", str(origin), "add", ".")
    git("-C", str(origin), "commit", "-qm", "c1")

    repo_dir = tmp_path / "cache" / "host" / "grp" / "app.git"
    repo_dir.parent.mkdir(parents=True)
    git("clone", "-q", "--bare", str(origin), str(repo_dir))

    def add_worktree(name):
        wt = repo_dir.parent / name
        git("-C", str(repo_dir), "worktree", "add", "--detach", str(wt))
        return wt

    cache = rc.RepoCache(cache_dir=str(tmp_path / "cache"))

    # default mode: worktree removed, bare repo kept for the next MR
    monkeypatch.setattr(rc.settings, "repo_cache_ephemeral", False)
    wt1 = add_worktree("app-mr1-aaa-wt")
    asyncio.run(cache.release(wt1))
    assert not wt1.exists() and repo_dir.exists()

    # ephemeral mode: bare repo dropped too
    monkeypatch.setattr(rc.settings, "repo_cache_ephemeral", True)
    wt2 = add_worktree("app-mr2-bbb-wt")
    asyncio.run(cache.release(wt2))
    assert not wt2.exists() and not repo_dir.exists()


# --- server queue dedupe ---

def test_review_queue_dedupe():
    async def run():
        queue = ReviewQueue(workers=0, dedupe_ttl=600)
        mr = {"gitlab_config": {"name": "primary"}, "project_id": 1,
              "mr_iid": 7, "last_commit": "abc"}
        assert queue.submit(dict(mr)) is True
        assert queue.submit(dict(mr)) is False          # webhook retry
        assert queue.submit({**mr, "last_commit": "def"}) is True  # new push
    asyncio.run(run())
