"""Notification events: what happened, as structured data.

No text and no markup here — every channel formats events its own way
(Telegram: Markdown; Bitrix24 later: its BB-code). Usage travels as a field,
so formatting never reads the usage tracker.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ReviewJob


@dataclass(frozen=True)
class MrSummary:
    instance: str  # instance name (primary, instance_2, ...)
    instance_url: str
    project_id: int
    project_path: str
    mr_iid: int
    url: str
    title: str = ""
    author: str = ""
    source_branch: str = ""
    target_branch: str = ""

    @classmethod
    def from_job(cls, job: ReviewJob) -> MrSummary:
        ref = job.ref
        return cls(instance=ref.instance.name, instance_url=ref.instance.url,
                   project_id=ref.project_id, project_path=ref.project_path,
                   mr_iid=ref.mr_iid, url=ref.url, title=job.title, author=job.author,
                   source_branch=job.source_branch, target_branch=job.target_branch)


@dataclass(frozen=True)
class ModelUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class UsageSummary:
    models: tuple[ModelUsage, ...] = ()
    total_cost_usd: float = 0.0


@dataclass(frozen=True)
class ReviewStarted:
    mr: MrSummary
    has_conflicts: bool = False
    job_id: str = ""
    language: str = ""  # "" = the channel's default


@dataclass(frozen=True)
class ReviewPosted:
    mr: MrSummary
    review_text: str
    has_conflicts: bool = False
    usage: UsageSummary | None = None
    job_id: str = ""
    language: str = ""


@dataclass(frozen=True)
class ReviewFailed:
    """A job failed; `details` is internal (exception text) — channels for the
    team only, never the MR."""
    kind: str  # gitlab_api_error | ai_failure | timeout | general
    details: str
    mr: MrSummary | None = None
    job_id: str = ""
    language: str = ""


@dataclass(frozen=True)
class TesterReportReady:
    mr: MrSummary
    filename: str
    content: bytes
    job_id: str = ""
    language: str = ""


@dataclass(frozen=True)
class SystemAlert:
    """Operational alert outside a review (webhook, startup, prompt cache)."""
    kind: str  # webhook_error | gitlab_api_error | prompt_cache | ...
    details: str
    instance: str = ""
    project_id: int | None = None
    mr_iid: int | None = None
    job_id: str = ""
    language: str = ""


NotificationEvent = ReviewStarted | ReviewPosted | ReviewFailed | TesterReportReady | SystemAlert
