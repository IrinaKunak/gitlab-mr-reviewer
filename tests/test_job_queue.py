"""Durable job queue (stage 18): accepted webhooks survive a restart, a job
runs at most JOB_MAX_ATTEMPTS times, a re-run never posts a second review,
and a graceful stop hands unfinished work back."""

from __future__ import annotations

import asyncio

import pytest

from reviewer.adapters.storage import Database
from reviewer.adapters.storage.jobs import FAILED, QUEUED, JobStore
from reviewer.bootstrap import build_services
from reviewer.server import ReviewQueue
from tests.factories import INSTANCE, dialogue_job, review_job
from tests.fakes import file_change, mr_webhook

RU_REVIEW_HEADER = "## 🤖 Автоматический обзор кода"


def _store(max_attempts=2):
    return JobStore(Database.memory(), {"primary": INSTANCE}, max_attempts=max_attempts)


def test_lifecycle_and_payload_has_no_secrets():
    from reviewer.domain.models import InstanceRef

    secret = InstanceRef("primary", "https://gitlab.test", "glpat-SECRET")
    store = JobStore(Database.memory(), {"primary": secret})
    job = review_job(job_id="j1", instance=secret, mr_iid=7, last_commit="abc",
                     force_full=True)
    store.enqueue(job)
    store.enqueue(dialogue_job(job_id="j2", instance=secret, note_id=5))
    (payload,) = store.db.query("SELECT payload FROM jobs WHERE job_id = 'j1'")[0]
    assert "glpat-SECRET" not in payload and '"instance": "primary"' in payload

    claimed = store.claim()
    assert claimed == job and claimed.attempt == 1  # round-trips, oldest first
    store.complete("j1")
    assert store.claim().note_id == 5
    assert store.claim() is None
    assert store.counts() == {"done": 1, "running": 1}


def test_failures_retry_until_out_of_attempts():
    store = _store(max_attempts=2)
    store.enqueue(review_job(job_id="j1"))
    store.fail(store.claim().job_id, "boom")
    retry = store.claim()
    assert retry.attempt == 2
    store.fail(retry.job_id, "boom again")
    assert store.claim() is None and store.counts() == {FAILED: 1}


def test_recover_requeues_interrupted_jobs():
    store = _store(max_attempts=2)
    for job_id in ("a", "b"):
        store.enqueue(review_job(job_id=job_id))
    store.claim()                       # a: killed on its 1st attempt
    store.fail(store.claim().job_id, "x")  # b: crashed once, re-queued ...
    store.claim()                       # ... and killed on its 2nd
    assert store.recover() == (1, 1)    # a back in the queue, b given up
    assert store.claim().job_id == "a"


def test_job_for_removed_instance_is_dropped():
    store = _store()
    store.enqueue(review_job(job_id="j1"))
    store.instances = {}  # instance removed from config.yaml before the restart
    assert store.claim() is None and store.counts() == {FAILED: 1}


def _restart(world):
    """A new process on the same state dir (the same fakes on the edges)."""
    return build_services(world.settings, telegram=world.telegram, ai=world.llm,
                          bridge=world.bridge, repo_cache=world.repo,
                          vcs_for=lambda instance: world.gitlab, workers=0)


def _trivial_round(world):
    world.llm.on_complete(
        {"complexity": "trivial", "risk_areas": [], "jira_keys": [],
         "needs_investigation": False, "summary": "s", "skip_globs": []},
        "Verdict: fine.", "Вердикт: всё в порядке.")


def _accept(world, project, mr):
    resp = world.client.post("/webhook", json=mr_webhook(project, mr), headers={
        "X-Gitlab-Token": "hook-token", "X-Gitlab-Event": "Merge Request Hook"})
    assert resp.json()["status"] == "accepted"


def _mr(world):
    project = world.gitlab.add_project(1, "group/app", files={"a.py": "x = 1\n"})
    return project, project.add_mr(7, changes=[file_change("a.py", "-x = 0\n+x = 1\n")])


def test_killed_mid_job_runs_exactly_once_after_restart(world):
    project, mr = _mr(world)
    _trivial_round(world)
    _accept(world, project, mr)
    world.services.queue.store.claim()  # a worker took it ... and the process died

    after = _restart(world)
    asyncio.run(after.queue.start())
    asyncio.run(after.queue.stop())
    assert asyncio.run(after.queue.drain()) == 1
    assert asyncio.run(after.queue.drain()) == 0  # nothing left over

    reviews = [n for n in mr.bot_notes if n.startswith(RU_REVIEW_HEADER)]
    assert len(reviews) == 1 and "Вердикт: всё в порядке." in reviews[0]
    assert after.queue.store.counts() == {"done": 1}


class Crash(BaseException):
    """The process dying (SIGKILL / OOM): nothing in the job can catch it."""


def test_crash_after_posting_does_not_post_twice(world, monkeypatch):
    project, mr = _mr(world)
    _trivial_round(world)  # scripted ONCE: a second review would be an AI call too many

    def die(*args, **kwargs):
        raise Crash()
    monkeypatch.setattr(world.services.review_state, "set_last_sha", die)
    _accept(world, project, mr)
    with pytest.raises(Crash):  # review posted, then the process died
        asyncio.run(world.services.queue.drain())

    after = _restart(world)
    after.queue.store.recover()
    asyncio.run(after.queue.drain())

    reviews = [n for n in mr.bot_notes if n.startswith(RU_REVIEW_HEADER)]
    assert len(reviews) == 1
    assert "<!-- mr-reviewer:review sha=sha-1 -->" in reviews[0]  # hidden marker
    assert after.review_state.get_last_sha("primary", 1, 7) == "sha-1"  # recorded now


def test_graceful_stop_waits_then_hands_back():
    class Runner:
        def __init__(self, seconds):
            self.seconds = seconds

        async def run(self, job):
            await asyncio.sleep(self.seconds)

    async def scenario(seconds, timeout):
        store = _store()
        queue = ReviewQueue(1, 600, 0, runner=Runner(seconds), store=store,
                            shutdown_timeout=timeout, poll_interval=0.01)
        await queue.start()
        queue.submit(review_job(mr_iid=1))
        await asyncio.sleep(0.05)  # the worker picks it up
        await queue.stop()
        return store

    finished = asyncio.run(scenario(0.1, timeout=5))  # finishes within the grace time
    assert finished.counts() == {"done": 1}

    cut = asyncio.run(scenario(30, timeout=0.1))  # too slow: re-queued, attempt not spent
    assert cut.counts() == {QUEUED: 1}
    assert cut.db.query("SELECT attempts FROM jobs")[0][0] == 0
