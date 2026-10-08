"""Routes a queued job to its use case (what a worker runs)."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import DialogueJob, Job, ReviewJob
from .answer_note import AnswerNote
from .review_mr import ReviewMergeRequest


@dataclass
class JobRunner:
    review_mr: ReviewMergeRequest
    answer_note: AnswerNote

    async def run(self, job: Job) -> None:
        if isinstance(job, DialogueJob):
            await self.answer_note.execute(job)
        elif isinstance(job, ReviewJob):
            await self.review_mr.execute(job)
