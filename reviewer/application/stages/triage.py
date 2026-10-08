"""Stage 1 — triage (fast tier): complexity, investigation need, Jira keys,
and which changed files are not worth reading (skip_globs)."""

from __future__ import annotations

import logging

from ... import prompts
from ...ai_client import AIError
from ...domain.models import ChangeSet, ReviewJob, Tier, TriageResult
from ...domain.skip import resolve_skip
from ...prompts import Prompts, default_prompts
from .. import content
from ..ports import LLMPort
from .base import ReviewContext

logger = logging.getLogger(__name__)


class Triage:
    def __init__(self, ai: LLMPort, templates: Prompts | None = None) -> None:
        self.ai = ai
        self.templates = templates or default_prompts()

    async def run(self, ctx: ReviewContext) -> ReviewContext:
        # triage runs FIRST: besides complexity it decides which changed files are
        # not worth reading (assets, generated output), so the expensive stages
        # never spend their budget on them
        ctx.triage = await self.classify(ctx.job, ctx.changes)
        ctx.skip = frozenset(resolve_skip(ctx.changes, ctx.triage.skip_globs))
        if ctx.skip:
            logger.info("triage skip_globs %s -> skipping contents of %d/%d files",
                        list(ctx.triage.skip_globs), len(ctx.skip), len(ctx.changes))
        return ctx

    async def classify(self, job: ReviewJob, changes: ChangeSet) -> TriageResult:
        diff_summary = content.extract_diff_only(changes)[:60_000]
        manifest = content.file_manifest(changes)
        fallback = TriageResult(jira_keys=tuple(content.extract_jira_keys(job)),
                                summary=job.title)
        try:
            parsed = await self.ai.complete_json(
                Tier.FAST, self.templates.TRIAGE_SYSTEM,
                prompts.triage_user_prompt(job, diff_summary, manifest),
                prompts.TRIAGE_SCHEMA)
        except AIError as exc:
            logger.warning("triage failed (%s) — defaulting to normal", exc)
            return fallback
        if not parsed or "complexity" not in parsed:
            logger.warning("triage returned unparseable output — defaulting to normal")
            return fallback
        # merge regex-found keys the model may have missed (lenient fallback parse
        # may return a non-list here — normalize instead of crashing the review)
        triage = TriageResult.from_model(parsed, fallback.jira_keys)
        logger.info("triage: complexity=%s investigate=%s jira=%s",
                    triage.complexity, triage.needs_investigation, list(triage.jira_keys))
        return triage
