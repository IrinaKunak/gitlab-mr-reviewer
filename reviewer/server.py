"""FastAPI app. Webhook contract is preserved from v1 byte-for-byte:

  POST /webhook  — X-Gitlab-Token routes to the instance, Merge Request Hook only,
                   open/update/reopen actions, returns {"status": "accepted", ...}
  GET  /         — health JSON {"status", "version"}

v2 changes: fire-and-forget BackgroundTasks replaced by an asyncio queue with
N workers and webhook-retry dedupe; the bridge listener runs as a lifespan task.

No module-level state: `create_app` gets the object graph (`bootstrap.Services`)
and the handlers read it from `request.app.state.services`.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .adapters.gitlab import parse_merge_request_webhook, parse_note_webhook
from .adapters.storage import Database
from .adapters.storage.jobs import JobStore
from .application.common import new_job_id
from .application.jobs import JobRunner
from .application.ports import JobQueue
from .config import ServerSection
from .dashboard import DASHBOARD_HTML
from .domain.dedupe import DedupePolicy
from .domain.events import SystemAlert
from .domain.models import Job, Tier

if TYPE_CHECKING:  # bootstrap imports this module; the type is all we need
    from .bootstrap import Services

logger = logging.getLogger(__name__)

UNKNOWN_TOKEN_ALERT_INTERVAL = 900  # unauthenticated requests must not drive TG spam


class ReviewQueue:
    """Bounded-concurrency job processing with webhook-retry dedupe, over a
    durable JobQueue: a webhook answered "accepted" is in the database before
    the response, so a deploy no longer drops it (#10)."""

    def __init__(self, workers: int, dedupe_ttl: int, burst_window: int = 30, *,
                 runner: JobRunner | None = None, store: JobQueue | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 shutdown_timeout: float = 20.0, poll_interval: float = 5.0) -> None:
        self.workers = workers
        self.runner = runner  # None only for a queue that never runs jobs
        # in-memory store for queues built without one (unit tests)
        self.store: JobQueue = store or JobStore(Database.memory(), {})
        self.dedupe = DedupePolicy(dedupe_ttl, burst_window)
        self.clock = clock
        self.shutdown_timeout = shutdown_timeout
        self.poll_interval = poll_interval
        self._wakeup = asyncio.Event()
        self._stopping = False
        self._tasks: list[asyncio.Task] = []

    def submit(self, job: Job) -> bool:
        """False if this exact MR state was queued recently (webhook retry / burst)."""
        if not self.dedupe.admit(job, self.clock()):
            return False
        # one id per queued job: log lines, TG alerts and the neutral MR
        # error note all carry it, so a user report maps back to the log
        job = replace(job, job_id=new_job_id())
        self.store.enqueue(job)
        self._wakeup.set()
        logger.info("job %s: queued %s for MR !%s", job.job_id, job.kind, job.ref.mr_iid)
        return True

    async def run(self, job: Job) -> None:
        """Process one job (what a worker does with it)."""
        assert self.runner is not None, "ReviewQueue without a runner cannot run jobs"
        await self.runner.run(job)

    async def process(self, job: Job) -> None:
        """Run one claimed job and record the outcome in the store."""
        try:
            await self.run(job)
        except asyncio.CancelledError:
            # shutdown timed out mid-job: hand it back, the next start resumes it
            self.store.release(job.job_id)
            raise
        except Exception as exc:  # noqa: BLE001 — workers must survive anything
            logger.exception("job %s: unhandled error", job.job_id)
            self.store.fail(job.job_id, str(exc))
        else:
            self.store.complete(job.job_id)

    async def drain(self) -> int:
        """Run queued jobs inline until none is left (tests, tools); the count."""
        done = 0
        while (job := self.store.claim()) is not None:
            await self.process(job)
            done += 1
        return done

    async def start(self) -> None:
        requeued, failed = self.store.recover()
        if requeued or failed:
            logger.warning("job queue: %d interrupted job(s) re-queued, %d given up "
                           "(out of attempts)", requeued, failed)
        self._stopping = False
        self._tasks = [asyncio.create_task(self._worker(i), name=f"review-worker-{i}")
                       for i in range(self.workers)]
        if requeued:
            self._wakeup.set()

    async def stop(self) -> None:
        """Graceful: no new jobs are taken; running ones get `shutdown_timeout`
        seconds to finish, then are cancelled and handed back to the queue."""
        self._stopping = True
        self._wakeup.set()
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=self.shutdown_timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if pending:
            logger.warning("shutdown: %d job(s) unfinished after %.0fs — re-queued for "
                           "the next start", len(pending), self.shutdown_timeout)

    async def _worker(self, idx: int) -> None:
        logger.info("review worker %d started", idx)
        while not self._stopping:
            try:
                job = self.store.claim()
            except Exception:  # noqa: BLE001 — a db hiccup must not kill the worker
                logger.exception("worker %d: cannot claim a job", idx)
                job = None
            if job is None:
                try:
                    await asyncio.wait_for(self._wakeup.wait(), self.poll_interval)
                except TimeoutError:
                    pass
                if not self._stopping:
                    self._wakeup.clear()
                continue
            if job.attempt > 1:
                logger.info("job %s: attempt %d", job.job_id, job.attempt)
            await self.process(job)


def create_app(services: Services | None = None, *,
               factory: Callable[[], Services] | None = None) -> FastAPI:
    """The FastAPI app over a built object graph.

    `services` is attached right away (tests drive the app without running the
    lifespan); otherwise the lifespan calls `factory` — config is then loaded
    at startup, not at import."""
    if services is None and factory is None:
        raise ValueError("create_app needs services or a factory")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "services", None) is None:
            assert factory is not None
            app.state.services = factory()
        svc: Services = app.state.services
        await svc.start()
        try:
            yield
        finally:
            await svc.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.services = services
    app.state.unknown_token_alert_at = -UNKNOWN_TOKEN_ALERT_INTERVAL
    app.include_router(router)
    return app


router = APIRouter()


def _svc(request: Request) -> Services:
    return request.app.state.services


@router.get("/")
async def root(request: Request) -> dict[str, Any]:
    stages = _svc(request).settings.pipeline.stages
    return {
        "status": "GitLab MR Reviewer is running",
        "version": __version__,
        "flags": {
            "investigator": stages.investigator,
            "bridge": _svc(request).settings.bridge.enabled,
            "tester_report": stages.tester_report,
            "review_repo_tools": stages.review_repo_tools,
            "dialogue": stages.dialogue,
        },
    }


def basic_auth_ok(cfg: ServerSection, authorization: str) -> bool:
    """Validate an HTTP Basic header against STATS_USER/STATS_PASSWORD."""
    if not (cfg.stats_user and cfg.stats_password):
        return False
    scheme, _, blob = authorization.partition(" ")
    if scheme.lower() != "basic" or not blob:
        return False
    try:
        user, _, password = base64.b64decode(blob.strip()).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return (hmac.compare_digest(user, cfg.stats_user)
            and hmac.compare_digest(password, cfg.stats_password))


def stats_access_allowed(cfg: ServerSection, authorization: str, query_token: str,
                         forwarded_for: str | None) -> bool:
    """Basic creds and/or STATS_TOKEN when configured; with neither configured,
    only direct local requests (proxied ones carry X-Forwarded-For) pass."""
    if basic_auth_ok(cfg, authorization):
        return True
    token = cfg.stats_token
    if token:
        if hmac.compare_digest(authorization, f"Bearer {token}"):
            return True
        return bool(query_token) and hmac.compare_digest(query_token, token)
    if cfg.stats_user and cfg.stats_password:
        return False  # basic auth is configured and did not match
    return forwarded_for is None


def _dash_guard(request: Request) -> None:
    """Shared auth for /stats, /dashboard and /admin endpoints."""
    cfg = _svc(request).settings.server
    if stats_access_allowed(
            cfg,
            request.headers.get("authorization", ""),
            request.query_params.get("token", ""),
            request.headers.get("x-forwarded-for")):
        return
    if cfg.stats_user and cfg.stats_password:
        # trigger the browser's native login prompt
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="mr-reviewer"'})
    raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    """Token/cost stats: overall totals, per-model breakdown, recent reviews."""
    _dash_guard(request)
    return await asyncio.to_thread(_svc(request).usage_log.aggregate)


@router.get("/dashboard")
async def dashboard(request: Request) -> HTMLResponse:
    """Self-contained stats dashboard (same auth as /stats)."""
    _dash_guard(request)
    return HTMLResponse(DASHBOARD_HTML)


@router.get("/admin/models")
async def get_models(request: Request) -> dict[str, Any]:
    """Current tier models: .env defaults, runtime overrides, effective values,
    plus the full OpenRouter catalog (any of which can be set as a tier override —
    vendor-prefixed ids route via OpenRouter, priced from the live catalog)."""
    _dash_guard(request)
    svc = _svc(request)
    ov = svc.overrides.load()
    defaults = {tier.value: svc.settings.model_for_tier(tier) for tier in Tier}
    catalog = await asyncio.to_thread(svc.catalog.refresh)
    or_models = [{"id": mid, "in": price[0], "out": price[1]}
                 for mid, price in sorted(catalog.items())]
    return {
        "defaults": defaults,
        "overrides": ov,
        "effective": {t: (ov.get(t) or d) for t, d in defaults.items()},
        "known_models": sorted(svc.pricing.prices),
        "known_prices": {m: list(p) for m, p in svc.pricing.prices.items()},
        "openrouter_models": or_models,
    }


@router.post("/admin/models")
async def set_models(request: Request) -> dict[str, Any]:
    """Set/clear per-tier model overrides (empty string = back to .env default).
    Applies immediately to new reviews; vendor-prefixed models run via OpenRouter."""
    _dash_guard(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected an object")
    saved = _svc(request).overrides.save(body)
    return {"overrides": saved}


async def _alert_unknown_token(request: Request, event_type: str | None,
                               token: str | None) -> None:
    now = time.monotonic()
    if now - request.app.state.unknown_token_alert_at < UNKNOWN_TOKEN_ALERT_INTERVAL:
        return
    request.app.state.unknown_token_alert_at = now
    svc = _svc(request)
    await svc.notifier.notify(SystemAlert(
        "webhook_error", f"Unknown webhook token received: {(token or '')[:10]}...",
        language=svc.settings.pipeline.language))


@router.post("/webhook")
async def handle_gitlab_webhook(request: Request):
    svc = _svc(request)
    event_type = request.headers.get("X-Gitlab-Event")
    gitlab_token = request.headers.get("X-Gitlab-Token")

    instance = svc.settings.gitlab.routes.get(gitlab_token or "")
    if not instance:
        logger.warning("No GitLab instance found for webhook token: %s",
                       (gitlab_token or "")[:10])
        await _alert_unknown_token(request, event_type, gitlab_token)
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook payload")
        await svc.notifier.notify(SystemAlert("webhook_error", "Invalid JSON in webhook payload",
                                              language=svc.settings.pipeline.language))
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    if event_type == "Note Hook":
        if not svc.settings.pipeline.stages.dialogue:
            return {"status": "ignored", "reason": "dialogue disabled"}
        note_job = parse_note_webhook(payload, instance)
        if not note_job:
            return {"status": "ignored", "reason": "not an MR comment"}
        bot = svc.vcs_for(instance).bot_username
        if bot and note_job.note_author == bot:
            return {"status": "ignored", "reason": "own note"}
        queued = svc.queue.submit(note_job)
        logger.info("%s dialogue for note %s on MR !%s in project %s on %s",
                    "Queued" if queued else "Deduped", note_job.note_id,
                    note_job.ref.mr_iid, note_job.ref.project_id, instance.name)
        return {
            "status": "accepted" if queued else "duplicate",
            "merge_request": note_job.ref.mr_iid,
            "instance": instance.name,
        }

    if event_type != "Merge Request Hook":
        logger.info("Ignoring non-MR event: %s", event_type)
        return {"status": "ignored", "reason": f"Not a merge request event: {event_type}"}

    try:
        review_job = parse_merge_request_webhook(payload, instance)
    except Exception as exc:  # noqa: BLE001
        # the caller is GitLab (its hook log is visible to project maintainers):
        # exception text stays in our log/alert, the response carries only an id
        job_id = new_job_id()
        logger.exception("job %s: error handling webhook", job_id)
        await svc.notifier.notify(SystemAlert("webhook_error", str(exc), job_id=job_id,
                                              language=svc.settings.pipeline.language))
        return JSONResponse(status_code=500,
                            content={"detail": "internal error", "job_id": job_id})

    if not review_job:
        return {"status": "ignored", "reason": "Invalid or unsupported MR action"}

    queued = svc.queue.submit(review_job)
    logger.info("%s quality check for MR !%s in project %s on %s",
                "Queued" if queued else "Deduped", review_job.ref.mr_iid,
                review_job.ref.project_id, instance.name)
    return {
        "status": "accepted" if queued else "duplicate",
        "merge_request": review_job.ref.mr_iid,
        "instance": instance.name,
    }
