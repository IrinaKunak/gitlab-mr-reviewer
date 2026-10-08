"""The job queue on SQLite (`jobs` table): accepted webhooks survive a deploy.

Lifecycle: queued -> running (claim, attempts + 1) -> done | queued again
(fail with attempts left) | failed. At startup `recover()` puts jobs a dead
process left `running` back in the queue — a job is tried at most
`max_attempts` times (a review that crashes the worker must not loop forever).
A graceful shutdown hands an unfinished job back without counting the attempt.

Jobs are stored without secrets: the GitLab instance is saved by name and
resolved against the configured instances on load (a job for an instance
removed from the config fails instead of running against nothing).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, replace

from ...domain.models import DialogueJob, InstanceRef, Job, JobKind, MergeRequestRef, ReviewJob
from .sqlite import Database

logger = logging.getLogger(__name__)

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
KEEP_FINISHED_SECONDS = 7 * 86_400  # done/failed rows kept for a week (debugging)
_CLASSES: dict[str, type[Job]] = {JobKind.REVIEW: ReviewJob, JobKind.DIALOGUE: DialogueJob}


def to_payload(job: Job) -> str:
    data = asdict(job)
    data["ref"]["instance"] = job.ref.instance.name  # never the token
    data.pop("attempt", None)
    return json.dumps(data, ensure_ascii=False)


def from_payload(kind: str, payload: str, instances: Mapping[str, InstanceRef],
                 attempt: int) -> Job:
    data = json.loads(payload)
    ref = data.pop("ref")
    instance = instances.get(ref["instance"])
    if instance is None:
        raise LookupError(f"GitLab instance {ref['instance']!r} is no longer configured")
    data["ref"] = MergeRequestRef(**{**ref, "instance": instance})
    job = _CLASSES[kind](**data)
    return replace(job, attempt=attempt)


class JobStore:
    """JobQueue port over the shared Database."""

    def __init__(self, db: Database, instances: Mapping[str, InstanceRef], *,
                 max_attempts: int = 2) -> None:
        self.db = db
        self.instances = instances  # name -> InstanceRef (live config)
        self.max_attempts = max_attempts

    def enqueue(self, job: Job) -> None:
        now = time.time()
        self.db.execute(
            "INSERT INTO jobs (job_id, kind, payload, status, attempts, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (job.job_id, str(job.kind), to_payload(job), QUEUED, now, now))

    def claim(self) -> Job | None:
        """The oldest queued job, now `running`; None when the queue is empty."""
        while True:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT job_id, kind, payload, attempts FROM jobs WHERE status = ? "
                    "ORDER BY created_at, rowid LIMIT 1", (QUEUED,)).fetchone()
                if row is None:
                    return None
                attempt = row["attempts"] + 1
                conn.execute("UPDATE jobs SET status = ?, attempts = ?, updated_at = ? "
                             "WHERE job_id = ?", (RUNNING, attempt, time.time(),
                                                  row["job_id"]))
            try:
                return from_payload(row["kind"], row["payload"], self.instances, attempt)
            except (LookupError, ValueError, TypeError, KeyError) as exc:
                logger.error("job %s cannot be loaded (%s) — dropped", row["job_id"], exc)
                self._finish(row["job_id"], FAILED, str(exc))

    def complete(self, job_id: str) -> None:
        self._finish(job_id, DONE)

    def fail(self, job_id: str, error: str) -> None:
        """A crash inside the job: retried while attempts are left."""
        rows = self.db.query("SELECT attempts FROM jobs WHERE job_id = ?", (job_id,))
        if rows and rows[0][0] < self.max_attempts:
            self.db.execute("UPDATE jobs SET status = ?, last_error = ?, updated_at = ? "
                            "WHERE job_id = ?", (QUEUED, error[:2000], time.time(), job_id))
        else:
            self._finish(job_id, FAILED, error)

    def release(self, job_id: str) -> None:
        """Graceful shutdown mid-job: back to the queue, the attempt not counted."""
        self.db.execute("UPDATE jobs SET status = ?, attempts = MAX(attempts - 1, 0), "
                        "updated_at = ? WHERE job_id = ? AND status = ?",
                        (QUEUED, time.time(), job_id, RUNNING))

    def recover(self) -> tuple[int, int]:
        """At startup: jobs left `running` by a dead process are re-queued (or
        failed when out of attempts). Returns (requeued, failed)."""
        now = time.time()
        with self.db.transaction() as conn:
            failed = conn.execute(
                "UPDATE jobs SET status = ?, last_error = 'interrupted (out of attempts)', "
                "updated_at = ? WHERE status = ? AND attempts >= ?",
                (FAILED, now, RUNNING, self.max_attempts)).rowcount
            requeued = conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE status = ?",
                (QUEUED, now, RUNNING)).rowcount
            conn.execute("DELETE FROM jobs WHERE status IN (?, ?) AND updated_at < ?",
                         (DONE, FAILED, now - KEEP_FINISHED_SECONDS))
        return requeued, failed

    def counts(self) -> dict[str, int]:
        return {row[0]: row[1] for row in
                self.db.query("SELECT status, COUNT(*) FROM jobs GROUP BY status")}

    def _finish(self, job_id: str, status: str, error: str = "") -> None:
        self.db.execute("UPDATE jobs SET status = ?, last_error = ?, updated_at = ? "
                        "WHERE job_id = ?", (status, error[:2000], time.time(), job_id))
