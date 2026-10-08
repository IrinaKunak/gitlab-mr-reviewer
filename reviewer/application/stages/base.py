"""What flows between the review stages.

A stage is `async run(ctx) -> ctx`: it reads what earlier stages put into the
`ReviewContext` and fills in its own part. The use case (ReviewMergeRequest)
decides which stages run and owns everything around them (MR preflight,
content assembly, the repo session, error handling).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ...domain.models import (
    ChangeSet,
    Investigation,
    MergeRequestInfo,
    ReviewJob,
    ReviewResult,
    TriageResult,
)
from ..ports import VcsPort

if TYPE_CHECKING:
    from ...ai_client import ToolDef


@dataclass
class ReviewContext:
    job: ReviewJob
    vcs: VcsPort
    mr: MergeRequestInfo
    changes: ChangeSet
    has_conflicts: bool = False
    # incremental re-review: `changes` is the prev_sha..head_sha delta
    incremental: bool = False
    prev_sha: str | None = None
    head_sha: str = ""
    # Triage
    triage: TriageResult = field(default_factory=TriageResult)
    skip: frozenset[str] = frozenset()
    # content (assembled by the use case after triage picked the skips)
    review_content: str = ""
    diff_only: str = ""
    system_extra: str = ""  # .ai-review.md guidelines + incremental note
    # repo session: tools over the MR head checkout (None = no checkout)
    repo_tools: list[ToolDef] | None = None
    use_review_tools: bool = False
    investigate: bool = False
    # Review / Investigate
    review: ReviewResult | None = None
    review_en: str = ""  # review text (+ impact analysis) before translation
    investigation: Investigation | None = None
    # Translate / Deliver
    review_out: str = ""
    posted: bool = False


class Stage(Protocol):
    async def run(self, ctx: ReviewContext) -> ReviewContext: ...
