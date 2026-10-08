"""Stage 2 — review: fast tier for trivial MRs, main tier otherwise; with the
repo tools when the session has a checkout (verify instead of hedging),
degrading the input instead of refusing a big MR."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from ... import prompts
from ...ai_client import CHARS_PER_TOKEN, AIError, AIInputTooLargeError, AIResult, ToolDef
from ...config import Settings
from ...domain import budget
from ...domain.models import ChangeSet, Complexity, ReviewJob, ReviewResult, Tier, TriageResult
from ...prompts import Prompts, default_prompts
from .. import content
from ..ports import LLMPort
from .base import ReviewContext

logger = logging.getLogger(__name__)


def mr_header(job: ReviewJob) -> str:
    return content.mr_header(job.title, job.author, job.source_branch, job.target_branch)


def review_user_prompts(settings: Settings, job: ReviewJob, review_content: str,
                        diff_only: str, changes: ChangeSet | None) -> Iterator[str]:
    """Successive smaller review inputs: full context -> diffs only -> as
    many whole file diffs as the budget fits. Each variant is built only
    after the previous one proved too large."""
    header = mr_header(job)
    trimmed = None
    if changes is not None:
        limit = budget.trim_budget_chars(settings.llm.max_input_tokens, CHARS_PER_TOKEN,
                                         budget.REVIEW_TRIM_SHARE)
        trimmed = lambda: content.extract_diff_only(changes, limit)  # noqa: E731
    ladder = budget.review_input_ladder(review_content, diff_only, trimmed)
    for step, (note, body) in enumerate(ladder):
        if step == 1:
            logger.warning("MR !%s too large with file context — retrying diff-only",
                           job.ref.mr_iid)
        elif step == 2:
            logger.warning("MR !%s still too large — reviewing a %d-char subset",
                           job.ref.mr_iid, len(body))
        yield prompts.review_user_prompt(header + note, body)


class Review:
    def __init__(self, settings: Settings, ai: LLMPort, templates: Prompts | None = None) -> None:
        self.settings = settings
        self.ai = ai
        self.templates = templates or default_prompts()

    async def run(self, ctx: ReviewContext) -> ReviewContext:
        ctx.review = await self.review(
            ctx.job, ctx.review_content, ctx.triage, ctx.diff_only, ctx.system_extra,
            ctx.changes, tools=ctx.repo_tools if ctx.use_review_tools else None)
        ctx.review_en = ctx.review.text
        return ctx

    async def review(self, job: ReviewJob, review_content: str, triage: TriageResult,
                     diff_only: str = "", system_extra: str = "",
                     changes: ChangeSet | None = None,
                     tools: list[ToolDef] | None = None) -> ReviewResult:
        if triage.complexity is Complexity.TRIVIAL:
            user = prompts.review_user_prompt(mr_header(job), review_content)
            result = await self.ai.complete(
                Tier.FAST, self.templates.TRIVIAL_REVIEW_SYSTEM + system_extra, user,
                max_tokens=1024)
            return ReviewResult(result.text)
        system = (self.settings.pipeline.review_prompt or self.templates.REVIEW_SYSTEM) + system_extra
        if triage.risk_areas:
            system += "\nTriage flagged risk areas: " + ", ".join(triage.risk_areas)
        if tools is not None:
            try:
                text = await self._review_with_tools(
                    system, job, review_content, diff_only, changes, tools)
                if text is not None:
                    return ReviewResult(text, tool_assisted=True)
            except AIInputTooLargeError:
                raise
            except AIError as exc:
                logger.warning("tool-assisted review failed (%s) — plain review", exc)
        result = await self._complete_with_degradation(
            Tier.MAIN, system, job, review_content, diff_only, effort="high",
            changes=changes)
        return ReviewResult(result.text)

    async def _complete_with_degradation(self, tier: Tier, system: str, job: ReviewJob,
                                         review_content: str, diff_only: str,
                                         effort: str | None = None,
                                         changes: ChangeSet | None = None) -> AIResult:
        """Review with file context, degrading rather than refusing."""
        last_exc: AIInputTooLargeError | None = None
        for user in review_user_prompts(self.settings, job, review_content, diff_only,
                                        changes):
            try:
                return await self.ai.complete(tier, system, user, max_tokens=16000,
                                              effort=effort)
            except AIInputTooLargeError as exc:
                last_exc = exc
        raise last_exc or AIInputTooLargeError("no review content variant fits")

    async def _review_with_tools(self, system: str, job: ReviewJob, review_content: str,
                                 diff_only: str, changes: ChangeSet | None,
                                 tools: list[ToolDef]) -> str | None:
        """Agentic review: same content ladder, plus read-only repo tools so the
        model VERIFIES cross-file concerns itself instead of asking the author
        to. Returns None when the loop produced no usable review — the caller
        falls back to the plain single-shot path."""
        max_calls = self.settings.pipeline.review_max_tool_calls
        system = system + self.templates.REVIEW_TOOLS_NOTE.format(max_calls=max_calls)
        last_exc: AIInputTooLargeError | None = None
        for user in review_user_prompts(self.settings, job, review_content, diff_only,
                                        changes):
            try:
                result = await self.ai.agent_loop(
                    Tier.MAIN, system, user, tools,
                    # the prompt budgets N tool calls; the loop needs turns for
                    # them plus a final text-only answer
                    max_iterations=max_calls + 2,
                    max_tokens=16000)
            except AIInputTooLargeError as exc:
                last_exc = exc
                continue
            if "verdict" not in result.text.lower():
                # ran out of turns mid-check or emitted only preamble — the
                # plain path writes the real review instead
                logger.warning("tool-assisted review produced no verdict — "
                               "falling back to plain review")
                return None
            logger.info("tool-assisted review done: in=%d cached=%d out=%d",
                        result.input_tokens,
                        result.cache_read_tokens + result.cache_creation_tokens,
                        result.output_tokens)
            return result.text
        raise last_exc or AIInputTooLargeError("no review content variant fits")
