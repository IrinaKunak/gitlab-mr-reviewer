"""FastAPI app. Webhook contract is preserved from v1 byte-for-byte:

  POST /webhook  — X-Gitlab-Token routes to the instance, Merge Request Hook only,
                   open/update/reopen actions, returns {"status": "accepted", ...}
  GET  /         — health JSON {"status", "version"}

v2 changes: fire-and-forget BackgroundTasks replaced by an asyncio queue with
N workers and webhook-retry dedupe; the bridge listener runs as a lifespan task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from . import __version__, gitlab_io, telegram_io, usage
from .bridge import bridge
from .config import settings
from .pipeline import pipeline

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
        now = time.monotonic()
        self._seen = {key: stamp for key, stamp in self._seen.items()
                      if now - stamp < self.dedupe_ttl}
        key = self.dedupe_key(mr_data)
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
            await asyncio.to_thread(gitlab_io.get_gitlab_client, config)
            logger.info("GitLab instance OK: %s (%s)", config["name"], config["url"])
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
                "provider=%s", settings.pipeline_v2, settings.investigator,
                settings.bridge_enabled, settings.tester_report, settings.ai_provider)
    if settings.ai_provider == "gemini" and not shutil.which("gemini"):
        logger.error("AI_PROVIDER=gemini but the gemini CLI is not installed — "
                     "this rollback path requires the v1 Docker image (master branch)")
    if settings.proxy_url:
        logger.info("Proxy: %s", settings.proxy_url)
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
        },
    }


@app.get("/stats")
async def stats() -> dict[str, Any]:
    """Token/cost stats: overall totals, per-model breakdown, recent reviews."""
    return await asyncio.to_thread(usage.aggregate)


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
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if event_type != "Merge Request Hook":
        logger.info("Ignoring non-MR event: %s", event_type)
        return {"status": "ignored", "reason": f"Not a merge request event: {event_type}"}

    try:
        mr_data = gitlab_io.parse_merge_request_webhook(payload)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error handling webhook: %s", exc)
        await telegram_io.notify_error("webhook_error", str(exc),
                                       {"event_type": event_type or "unknown"})
        raise HTTPException(status_code=500, detail=str(exc))

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
