"""Stage 6 — delivery: the review comment in the MR plus the team
notification; the tester report (attachment + note + documents to chats)."""

from __future__ import annotations

import logging

from ...config import Settings
from ...domain.events import MrSummary, ReviewFailed, ReviewPosted, TesterReportReady
from ...domain.models import Tier
from ...i18n import t
from .. import content
from ..common import bot_username
from ..ports import KnowledgeSource, Notifier
from .base import ReviewContext
from .translate import Translator

logger = logging.getLogger(__name__)


class Deliver:
    """Posts the review; `ctx.posted` drives the last-reviewed-sha state for
    incremental re-reviews."""

    def __init__(self, settings: Settings, notifier: Notifier) -> None:
        self.settings = settings
        self.notifier = notifier

    async def run(self, ctx: ReviewContext) -> ReviewContext:
        job, ref = ctx.job, ctx.job.ref
        lang = self.settings.pipeline.language
        if await self._already_posted(ctx):
            logger.info("job %s: review for %s already in MR !%s — not posting again",
                        job.job_id, ctx.head_sha[:8], ref.mr_iid)
            ctx.posted = True
            return ctx
        comment = content.format_review_comment(ctx.review_out, lang)
        if ctx.head_sha:
            comment += content.review_marker(ctx.head_sha) + "\n"
        try:
            await ctx.vcs.post_note(ref, comment)
            logger.info("Posted review for MR !%s", ref.mr_iid)
        except Exception as exc:  # noqa: BLE001
            job_id = job.job_id or "?"
            logger.error("job %s: failed to post review comment: %s", job_id, exc)
            await self.notifier.notify(ReviewFailed(
                "gitlab_api_error", f"Failed to post review comment: {exc}",
                mr=MrSummary.from_job(job), job_id=job_id, language=lang))
            try:
                await ctx.vcs.post_note(ref, t("mr.post_failed", lang, job_id=job_id))
            except Exception:  # noqa: BLE001
                logger.error("Failed to post error message as well")
            ctx.posted = False
            return ctx
        await self.notifier.notify(ReviewPosted(
            MrSummary.from_job(job), ctx.review_out, ctx.has_conflicts,
            usage=ctx.usage.summary() if ctx.usage is not None else None,
            job_id=job.job_id, language=lang))
        ctx.posted = True
        return ctx


    @staticmethod
    async def _already_posted(ctx: ReviewContext) -> bool:
        """Idempotent publishing (FT-2 p.4 / stage 18.3): a re-run of the job
        after a crash between posting and recording must not post twice. An
        explicit re-review (force_full) always posts."""
        if ctx.job.force_full or not ctx.head_sha:
            return False
        try:
            notes = await ctx.vcs.list_notes(ctx.job.ref)
            bot = await bot_username(ctx.vcs)
        except Exception as exc:  # noqa: BLE001 — cannot check: post (v1 behavior)
            logger.debug("idempotency check skipped: %s", exc)
            return False
        return content.has_review_for(notes, ctx.head_sha, bot)


class DeliverTesterReport:
    """Runs after the review is out (and its sha recorded): the report is
    translated on the main tier — it is long, Haiku leaves it half-English."""

    def __init__(self, settings: Settings, notifier: Notifier, knowledge: KnowledgeSource,
                 translator: Translator) -> None:
        self.settings = settings
        self.notifier = notifier
        self.knowledge = knowledge
        self.translator = translator

    async def run(self, ctx: ReviewContext) -> ReviewContext:
        investigation = ctx.investigation
        if not (investigation and self.settings.pipeline.stages.tester_report
                and investigation.tester_report):
            return ctx
        report = await self.translator.translate(investigation.tester_report, tier=Tier.MAIN)
        ref = ctx.job.ref
        filename = (f"tester-report-{ref.project_path.replace('/', '-')}"
                    f"-MR{ref.mr_iid}.md")
        link = await ctx.vcs.upload(ref, filename, report.encode("utf-8"))
        if link:
            await ctx.vcs.post_note(
                ref, t("mr.tester_report", self.settings.pipeline.language, link=link))
        else:  # upload failed — inline the report so it isn't lost
            await ctx.vcs.post_note(ref, report[:60_000])

        # AIManager archives reports into its corpus (the bridge chat), then
        # the team channels where the testers actually are
        content_bytes = report.encode("utf-8")
        await self.knowledge.archive(
            filename, content_bytes,
            f"🧪 Tester report: {ref.project_path} !{ref.mr_iid}\n{ref.url}")
        await self.notifier.notify(TesterReportReady(
            MrSummary.from_job(ctx.job), filename, content_bytes, job_id=ctx.job.job_id,
            language=self.settings.pipeline.language))
        return ctx
