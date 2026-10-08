"""Logging: one place that configures it, and the job a log line belongs to.

`JobContext` (job_id, instance, project, mr_iid, kind) lives in a contextvar
for the duration of a job; `JobContextFilter` stamps it on every record, so
the lines of two reviews running in parallel workers can be told apart:

    2026-10-08 12:00:01 - reviewer.ai_client - INFO - [a1b2c3d4 primary group/app!7 review] ...

The same context carries the job's usage tracker (it replaced the separate
usage contextvar), so every AI call is accounted to the job it ran for.
`configure()` also sets up the AI request/response debug log (AI_DEBUG).
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .domain.models import Job
    from .usage import UsageTracker

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(job)s%(message)s"
AI_DEBUG_LOGGER = "reviewer.ai_debug"
AI_DEBUG_FILE = "ai-debug.log"


@dataclass(frozen=True)
class JobContext:
    job_id: str = ""
    instance: str = ""
    project: str = ""
    mr_iid: int | None = None
    kind: str = ""
    usage: UsageTracker | None = field(default=None, compare=False, repr=False)

    @classmethod
    def of(cls, job: Job, usage: UsageTracker | None = None) -> JobContext:
        return cls(job_id=job.job_id, instance=job.ref.instance.name,
                   project=job.ref.project_path, mr_iid=job.ref.mr_iid,
                   kind=str(job.kind), usage=usage)

    @property
    def label(self) -> str:
        parts = [self.job_id, self.instance]
        if self.project or self.mr_iid is not None:
            parts.append(f"{self.project}!{self.mr_iid}")
        parts.append(self.kind)
        return " ".join(p for p in parts if p)


_current: contextvars.ContextVar[JobContext | None] = contextvars.ContextVar(
    "job_context", default=None)


def current_job() -> JobContext | None:
    return _current.get()


@contextmanager
def job_context(job: Job | None = None, *,
                usage: UsageTracker | None = None) -> Iterator[JobContext]:
    """The job (and its usage tracker) for everything run inside the block —
    including asyncio tasks it starts (they copy the context)."""
    ctx = JobContext.of(job, usage) if job is not None else JobContext(usage=usage)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


class JobContextFilter(logging.Filter):
    """Adds `record.job` ("[label] " inside a job, "" outside) and the raw fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _current.get()
        label = ctx.label if ctx is not None else ""
        record.job = f"[{label}] " if label else ""
        record.job_id = ctx.job_id if ctx is not None else ""
        return True


def configure(settings: Settings) -> None:
    """Root logging (format, level) + the job filter on every root handler, and
    the AI debug log when AI_DEBUG is on."""
    logging.basicConfig(level=logging.DEBUG if settings.server.debug else logging.INFO,
                        format=LOG_FORMAT)
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, JobContextFilter) for f in handler.filters):
            handler.addFilter(JobContextFilter())
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
    if settings.llm.debug:
        configure_ai_debug(settings.storage.log_dir)


def configure_ai_debug(log_dir: str | Path) -> logging.Logger | None:
    """logs/ai-debug.log (rotating, 3 x 20 MB): AI request/response dumps.
    Fail-open — an unwritable logs/ mount must never take down a review: it
    warns once and the dumps are dropped."""
    debug = logging.getLogger(AI_DEBUG_LOGGER)
    debug.propagate = False  # payloads never reach the main log
    debug.setLevel(logging.DEBUG)
    if debug.handlers:
        return debug
    try:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path / AI_DEBUG_FILE, maxBytes=20_000_000,
                                      backupCount=3, encoding="utf-8")
    except OSError as exc:
        logger.warning("AI debug logging disabled (%s) — reviews continue without it", exc)
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(job)s%(message)s"))
    handler.addFilter(JobContextFilter())
    debug.addHandler(handler)
    return debug
