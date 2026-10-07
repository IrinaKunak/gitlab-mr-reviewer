"""FastAPI app. Webhook contract is preserved from v1 byte-for-byte:

  POST /webhook  — X-Gitlab-Token routes to the instance, Merge Request Hook only,
                   open/update/reopen actions, returns {"status": "accepted", ...}
  GET  /         — health JSON {"status", "version"}

v2 changes: fire-and-forget BackgroundTasks replaced by an asyncio queue with
N workers and webhook-retry dedupe; the bridge listener runs as a lifespan task.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__, gitlab_io, openrouter_models, overrides, state_layout, telegram_io, usage
from .bridge import bridge
from .config import settings
from .pipeline import new_job_id, pipeline

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class ReviewQueue:
    """Bounded-concurrency MR processing with webhook-retry dedupe."""

    def __init__(self, workers: int, dedupe_ttl: int, burst_window: int = 30):
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.workers = workers
        self.dedupe_ttl = dedupe_ttl
        self.burst_window = burst_window
        self._seen: dict[tuple, float] = {}
        self._mr_seen: dict[tuple, float] = {}
        self._tasks: list[asyncio.Task] = []

    def dedupe_key(self, mr_data: dict) -> tuple:
        return (mr_data["gitlab_config"]["name"], mr_data["project_id"],
                mr_data["mr_iid"], mr_data.get("last_commit"))

    def submit(self, mr_data: dict) -> bool:
        """Returns False if this exact MR state was queued recently (webhook retry)."""
        # one id per queued job: log lines, TG alerts and the neutral MR
        # error note all carry it, so a user report maps back to the log
        mr_data["job_id"] = new_job_id()
        accepted = self._submit(mr_data)
        if accepted:
            logger.info("job %s: queued %s for MR !%s", mr_data["job_id"],
                        mr_data.get("kind") or "review", mr_data.get("mr_iid"))
        return accepted

    def _submit(self, mr_data: dict) -> bool:
        now = time.monotonic()
        self._seen = {key: stamp for key, stamp in self._seen.items()
                      if now - stamp < self.dedupe_ttl}
        key = self.dedupe_key(mr_data)
        if mr_data.get("kind") == "note":
            # dialogue job: dedupe purely by note id (webhook retries) — the
            # per-MR burst window must NOT apply, a reply right after a review
            # event is exactly the case we want to serve
            note_key = ("note", key[0], key[1], mr_data.get("note_id"))
            if note_key in self._seen:
                logger.info("Duplicate note webhook for %s — skipped", note_key)
                return False
            self._seen[note_key] = now
            self.queue.put_nowait(mr_data)
            return True
        if mr_data.get("force_full"):
            # explicit re-review request — bypass dedupe (the triggering label
            # event carries the same sha the TTL window would swallow)
            self._seen[key] = now
            self._mr_seen[key[:3]] = now
            self.queue.put_nowait(mr_data)
            return True
        if key in self._seen:
            logger.info("Duplicate webhook for %s — skipped", key)
            return False
        # one user action can emit several events with different shas (e.g.
        # reopen + update after new commits) — collapse the burst per MR; the
        # queued review reads live MR state anyway, so nothing is lost
        mr_key = key[:3]
        last = self._mr_seen.get(mr_key)
        if last is not None and now - last < self.burst_window:
            logger.info("Burst duplicate for %s — skipped", mr_key)
            return False
        self._mr_seen = {k: s for k, s in self._mr_seen.items()
                         if now - s < self.burst_window}
        self._seen[key] = now
        self._mr_seen[mr_key] = now
        self.queue.put_nowait(mr_data)
        return True

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker(i), name=f"review-worker-{i}")
                       for i in range(self.workers)]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _worker(self, idx: int) -> None:
        logger.info("review worker %d started", idx)
        while True:
            mr_data = await self.queue.get()
            try:
                if mr_data.get("kind") == "note":
                    await pipeline.process_note(mr_data)
                else:
                    await pipeline.process(mr_data)
            except Exception:  # noqa: BLE001 — workers must survive anything
                logger.exception("worker %d: unhandled pipeline error", idx)
            finally:
                self.queue.task_done()


review_queue = ReviewQueue(settings.ai_workers, settings.dedupe_ttl,
                           settings.dedupe_burst)

_last_unknown_token_alert = 0.0
_UNKNOWN_TOKEN_ALERT_INTERVAL = 900  # unauthenticated requests must not drive TG spam


async def _alert_unknown_token(event_type: str | None, token: str | None) -> None:
    global _last_unknown_token_alert
    now = time.monotonic()
    if now - _last_unknown_token_alert < _UNKNOWN_TOKEN_ALERT_INTERVAL:
        return
    _last_unknown_token_alert = now
    await telegram_io.notify_error(
        "webhook_error", f"Unknown webhook token received: {(token or '')[:10]}...",
        {"event_type": event_type})


async def _verify_instances() -> None:
    """Startup connectivity check (non-fatal, v1 behavior)."""
    for config in settings.gitlab_instances.values():
        try:
            gl = await asyncio.to_thread(gitlab_io.get_gitlab_client, config)
            # remembered so note webhooks from the bot itself are dropped at
            # the door instead of queueing a job (every review post fires one)
            config["bot_username"] = getattr(
                getattr(gl, "user", None), "username", "") or ""
            logger.info("GitLab instance OK: %s (%s), bot=%s", config["name"],
                        config["url"], config["bot_username"] or "?")
        except Exception as exc:  # noqa: BLE001
            logger.error("GitLab instance %s connection failed: %s", config["name"], exc)
            await telegram_io.notify_error(
                "gitlab_api_error", f"Startup connection failed: {exc}",
                {"gitlab_instance": config["name"]})


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GitLab MR Reviewer v%s starting", __version__)
    logger.info("Instances: %s", [c["name"] for c in settings.gitlab_instances.values()])
    logger.info("Flags: pipeline_v2=%s investigator=%s bridge=%s tester_report=%s "
                "review_repo_tools=%s dialogue=%s provider=%s",
                settings.pipeline_v2, settings.investigator,
                settings.bridge_enabled, settings.tester_report,
                settings.review_repo_tools, settings.dialogue_enabled,
                settings.ai_provider)
    if settings.ai_provider == "openrouter" and not settings.openrouter_token:
        logger.error("AI_PROVIDER=openrouter but OPENROUTER_API_TOKEN is empty")
    if settings.ai_provider == "gemini" and not shutil.which("gemini"):
        logger.error("AI_PROVIDER=gemini but the gemini CLI is not installed — "
                     "this rollback path requires the v1 Docker image (master branch)")
    if settings.proxy_url:
        logger.info("Proxy: %s", settings.proxy_url)
    state_layout.migrate(settings)  # before anything reads overrides/review state
    await review_queue.start()
    await bridge.start()
    verify_task = asyncio.create_task(_verify_instances())
    yield
    verify_task.cancel()
    await bridge.stop()
    await review_queue.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "status": "GitLab MR Reviewer is running",
        "version": __version__,
        "flags": {
            "pipeline_v2": settings.pipeline_v2,
            "investigator": settings.investigator,
            "bridge": settings.bridge_enabled,
            "tester_report": settings.tester_report,
            "review_repo_tools": settings.review_repo_tools,
            "dialogue": settings.dialogue_enabled,
        },
    }


def basic_auth_ok(authorization: str) -> bool:
    """Validate an HTTP Basic header against STATS_USER/STATS_PASSWORD."""
    if not (settings.stats_user and settings.stats_password):
        return False
    scheme, _, blob = authorization.partition(" ")
    if scheme.lower() != "basic" or not blob:
        return False
    try:
        user, _, password = base64.b64decode(blob.strip()).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return (hmac.compare_digest(user, settings.stats_user)
            and hmac.compare_digest(password, settings.stats_password))


def stats_access_allowed(authorization: str, query_token: str,
                         forwarded_for: str | None) -> bool:
    """Basic creds and/or STATS_TOKEN when configured; with neither configured,
    only direct local requests (proxied ones carry X-Forwarded-For) pass."""
    if basic_auth_ok(authorization):
        return True
    token = settings.stats_token
    if token:
        if hmac.compare_digest(authorization, f"Bearer {token}"):
            return True
        return bool(query_token) and hmac.compare_digest(query_token, token)
    if settings.stats_user and settings.stats_password:
        return False  # basic auth is configured and did not match
    return forwarded_for is None


def _dash_guard(request: Request) -> None:
    """Shared auth for /stats, /dashboard and /admin endpoints."""
    if stats_access_allowed(
            request.headers.get("authorization", ""),
            request.query_params.get("token", ""),
            request.headers.get("x-forwarded-for")):
        return
    if settings.stats_user and settings.stats_password:
        # trigger the browser's native login prompt
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="mr-reviewer"'})
    raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    """Token/cost stats: overall totals, per-model breakdown, recent reviews."""
    _dash_guard(request)
    return await asyncio.to_thread(usage.aggregate)


@app.get("/dashboard")
async def dashboard(request: Request) -> HTMLResponse:
    """Self-contained stats dashboard (same auth as /stats)."""
    _dash_guard(request)
    from .dashboard import DASHBOARD_HTML
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/admin/models")
async def get_models(request: Request) -> dict[str, Any]:
    """Current tier models: .env defaults, runtime overrides, effective values,
    plus the full OpenRouter catalog (any of which can be set as a tier override —
    vendor-prefixed ids route via OpenRouter, priced from the live catalog)."""
    _dash_guard(request)
    ov = overrides.load()
    defaults = {"fast": settings.model_fast, "main": settings.model_main,
                "smart": settings.model_smart}
    catalog = await asyncio.to_thread(openrouter_models.refresh)
    or_models = [{"id": mid, "in": price[0], "out": price[1]}
                 for mid, price in sorted(catalog.items())]
    return {
        "defaults": defaults,
        "overrides": ov,
        "effective": {t: (ov.get(t) or d) for t, d in defaults.items()},
        "known_models": sorted(usage.PRICES),
        "known_prices": {m: list(p) for m, p in usage.PRICES.items()},
        "openrouter_models": or_models,
    }


@app.post("/admin/models")
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
    saved = overrides.save(body)
    return {"overrides": saved}


@app.post("/webhook")
async def handle_gitlab_webhook(request: Request):
    event_type = request.headers.get("X-Gitlab-Event")
    gitlab_token = request.headers.get("X-Gitlab-Token")

    gitlab_config = settings.gitlab_instances.get(gitlab_token or "")
    if not gitlab_config:
        logger.warning("No GitLab instance found for webhook token: %s",
                       (gitlab_token or "")[:10])
        await _alert_unknown_token(event_type, gitlab_token)
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook payload")
        await telegram_io.notify_error("webhook_error", "Invalid JSON in webhook payload")
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    if event_type == "Note Hook":
        if not settings.dialogue_enabled:
            return {"status": "ignored", "reason": "dialogue disabled"}
        note_data = gitlab_io.parse_note_webhook(payload)
        if not note_data:
            return {"status": "ignored", "reason": "not an MR comment"}
        bot = gitlab_config.get("bot_username", "")
        if bot and note_data.get("note_author") == bot:
            return {"status": "ignored", "reason": "own note"}
        note_data["gitlab_config"] = gitlab_config
        accepted = review_queue.submit(note_data)
        logger.info("%s dialogue for note %s on MR !%s in project %s on %s",
                    "Queued" if accepted else "Deduped", note_data["note_id"],
                    note_data["mr_iid"], note_data["project_id"],
                    gitlab_config["name"])
        return {
            "status": "accepted" if accepted else "duplicate",
            "merge_request": note_data["mr_iid"],
            "instance": gitlab_config["name"],
        }

    if event_type != "Merge Request Hook":
        logger.info("Ignoring non-MR event: %s", event_type)
        return {"status": "ignored", "reason": f"Not a merge request event: {event_type}"}

    try:
        mr_data = gitlab_io.parse_merge_request_webhook(payload)
    except Exception as exc:  # noqa: BLE001
        # the caller is GitLab (its hook log is visible to project maintainers):
        # exception text stays in our log/alert, the response carries only an id
        job_id = new_job_id()
        logger.exception("job %s: error handling webhook", job_id)
        await telegram_io.notify_error("webhook_error", str(exc),
                                       {"event_type": event_type or "unknown",
                                        "job_id": job_id})
        return JSONResponse(status_code=500,
                            content={"detail": "internal error", "job_id": job_id})

    if not mr_data:
        return {"status": "ignored", "reason": "Invalid or unsupported MR action"}

    mr_data["gitlab_config"] = gitlab_config
    accepted = review_queue.submit(mr_data)
    logger.info("%s quality check for MR !%s in project %s on %s",
                "Queued" if accepted else "Deduped", mr_data["mr_iid"],
                mr_data["project_id"], gitlab_config["name"])
    return {
        "status": "accepted" if accepted else "duplicate",
        "merge_request": mr_data["mr_iid"],
        "instance": gitlab_config["name"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
