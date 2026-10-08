"""HTTP entry point: webhook handling, /stats auth, the review queue, the entry point."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from reviewer.server import ReviewQueue
from tests.factories import (
    INSTANCE,
    make_services,
    make_settings,
    review_job,
)


def test_basic_auth(monkeypatch):
    import base64

    from reviewer.config import ServerSection
    from reviewer.server import basic_auth_ok, stats_access_allowed

    cfg = ServerSection(stats_user="max", stats_password="pw123")
    good = "Basic " + base64.b64encode(b"max:pw123").decode()
    bad = "Basic " + base64.b64encode(b"max:nope").decode()
    assert basic_auth_ok(cfg, good) is True
    assert basic_auth_ok(cfg, bad) is False
    assert basic_auth_ok(cfg, "Bearer xyz") is False
    # with basic configured, unauthenticated local access is no longer allowed
    assert stats_access_allowed(cfg, good, "", "203.0.113.7") is True
    assert stats_access_allowed(cfg, "", "", None) is False


def test_stats_access_control(monkeypatch):
    from reviewer.config import ServerSection
    from reviewer.server import stats_access_allowed

    # no token configured: only direct (non-proxied) requests pass
    cfg = ServerSection()
    assert stats_access_allowed(cfg, "", "", None) is True
    assert stats_access_allowed(cfg, "", "", "203.0.113.7") is False

    # token configured: Bearer header or ?token= must match exactly
    cfg = ServerSection(stats_token="s3cret")
    assert stats_access_allowed(cfg, "Bearer s3cret", "", "203.0.113.7") is True
    assert stats_access_allowed(cfg, "", "s3cret", "203.0.113.7") is True
    assert stats_access_allowed(cfg, "Bearer wrong", "", None) is False
    assert stats_access_allowed(cfg, "", "", None) is False  # token set: local needs it too


_LEAKY = "connect to http://10.0.0.5:8080/internal failed, see /srv/app/secrets.py"


def test_review_queue_assigns_job_id():
    async def run():
        from reviewer.adapters.storage import Database
        from reviewer.adapters.storage.jobs import JobStore

        queue = ReviewQueue(workers=0, dedupe_ttl=600, burst_window=0,
                            store=JobStore(Database.memory(), {"primary": INSTANCE}))
        job = review_job(mr_iid=7, last_commit="abc")
        assert queue.submit(job) is True
        queued = queue.store.claim()
        assert len(queued.job_id) == 8
        assert queue.submit(replace(job, last_commit="def")) is True
        assert queue.store.claim().job_id != queued.job_id
    asyncio.run(run())


def test_webhook_500_hides_exception_text(monkeypatch):

    from fastapi.testclient import TestClient

    from reviewer import server

    def boom(payload, instance):
        raise ValueError(_LEAKY)

    monkeypatch.setattr(server, "parse_merge_request_webhook", boom)
    cfg = make_settings(gitlab__routes={"hook": INSTANCE})
    svc = make_services(cfg)
    resp = TestClient(server.create_app(svc)).post(
        "/webhook", json={"object_kind": "merge_request"},
        headers={"X-Gitlab-Token": "hook", "X-Gitlab-Event": "Merge Request Hook"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"] == "internal error"
    assert len(body["job_id"]) == 8
    assert "10.0.0.5" not in resp.text and "/srv/app" not in resp.text


def test_single_entry_point_runs_uvicorn_on_port_5000(monkeypatch):
    import uvicorn
    from fastapi import FastAPI

    from reviewer import __main__ as entry
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    # config is loaded (and validated) before uvicorn starts; no .env in tests
    monkeypatch.setattr(entry, "load_config", lambda: make_settings())
    entry.main()
    [(args, kwargs)] = calls
    assert isinstance(args[0], FastAPI) and args[0].state.services is not None
    assert kwargs == {"host": "0.0.0.0", "port": 5000}
