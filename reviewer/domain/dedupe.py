"""Webhook dedupe policy: which incoming jobs get queued.

Pure state machine over (job, monotonic now); the queue owns the clock.
"""

from __future__ import annotations

import logging

from .models import DialogueJob, Job, ReviewJob

logger = logging.getLogger(__name__)


class DedupePolicy:
    def __init__(self, ttl: float, burst_window: float = 30) -> None:
        self.ttl = ttl
        self.burst_window = burst_window
        self._seen: dict[tuple, float] = {}
        self._mr_seen: dict[tuple, float] = {}

    @staticmethod
    def key(job: Job) -> tuple:
        return (*job.ref.key, job.last_commit)

    def admit(self, job: Job, now: float) -> bool:
        """False if this exact MR state was queued recently (webhook retry)."""
        self._seen = {key: stamp for key, stamp in self._seen.items()
                      if now - stamp < self.ttl}
        key = self.key(job)
        if isinstance(job, DialogueJob):
            # dialogue job: dedupe purely by note id (webhook retries) — the
            # per-MR burst window must NOT apply, a reply right after a review
            # event is exactly the case we want to serve
            note_key = ("note", key[0], key[1], job.note_id)
            if note_key in self._seen:
                logger.info("Duplicate note webhook for %s — skipped", note_key)
                return False
            self._seen[note_key] = now
            return True
        mr_key = key[:3]
        if isinstance(job, ReviewJob) and job.force_full:
            # explicit re-review request — bypass dedupe (the triggering label
            # event carries the same sha the TTL window would swallow)
            self._seen[key] = now
            self._mr_seen[mr_key] = now
            return True
        if key in self._seen:
            logger.info("Duplicate webhook for %s — skipped", key)
            return False
        # one user action can emit several events with different shas (e.g.
        # reopen + update after new commits) — collapse the burst per MR; the
        # queued review reads live MR state anyway, so nothing is lost
        last = self._mr_seen.get(mr_key)
        if last is not None and now - last < self.burst_window:
            logger.info("Burst duplicate for %s — skipped", mr_key)
            return False
        self._mr_seen = {k: s for k, s in self._mr_seen.items()
                         if now - s < self.burst_window}
        self._seen[key] = now
        self._mr_seen[mr_key] = now
        return True
