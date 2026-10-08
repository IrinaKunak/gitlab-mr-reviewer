"""Stage 6 — delivery: the review comment in the MR plus the team
notification; the tester report (attachment + note + documents to chats)."""

from __future__ import annotations

import logging
from typing import Any

from ...config import Settings
from ...domain.models import Tier
from .. import content
from ..messages import POST_FAILED_MSG, TESTER_REPORT_COMMENT, msg
from .base import ReviewContext
from .translate import Translator

logger = logging.getLogger(__name__)


def tester_report_targets(settings: Settings) -> list[str]:
    """Chats that receive the tester-report document.

    Bridge chat first (AIManager archives reports into its corpus), then the
    team channels where the testers actually are (TESTER_REPORT_CHAT_IDS, or
    all regular notification channels when unset)."""
    targets: list[str] = []
    if settings.bridge.chat_id:
        targets.append(settings.bridge.chat_id)
    if settings.notify.telegram.enabled:
        for chat_id in settings.notify.telegram.tester_report_chat_ids or settings.notify.telegram.chat_ids:
            if chat_id not in targets:
                targets.append(chat_id)
    return targets


class Deliver:
    """Posts the review; `ctx.posted` drives the last-reviewed-sha state for
    incremental re-reviews."""

    def __init__(self, settings: Settings, telegram: Any) -> None:
        self.settings = settings
        self.telegram = telegram

    async def run(self, ctx: ReviewContext) -> ReviewContext:
        job, ref = ctx.job, ctx.job.ref
        lang = self.settings.pipeline.language
        comment = content.format_review_comment(ctx.review_out, lang)
        try:
            await ctx.vcs.post_note(ref, comment)
            logger.info("Posted review for MR !%s", ref.mr_iid)
        except Exception as exc:  # noqa: BLE001
            job_id = job.job_id or "?"
            logger.error("job %s: failed to post review comment: %s", job_id, exc)
            await self.telegram.notify_error(
                "gitlab_api_error", f"Failed to post review comment: {exc}",
                {"project_id": ref.project_id, "mr_iid": ref.mr_iid,
                 "gitlab_instance": ref.instance.name, "job_id": job_id})
            try:
                await ctx.vcs.post_note(ref, msg(POST_FAILED_MSG, lang, job_id=job_id))
            except Exception:  # noqa: BLE001
                logger.error("Failed to post error message as well")
            ctx.posted = False
            return ctx
        if self.settings.notify.telegram.enabled:
            await self.telegram.notify(self.telegram.format_mr_message(
                job, ref.project_path, ctx.has_conflicts, ctx.review_out, ref.instance.url))
        ctx.posted = True
        return ctx


class DeliverTesterReport:
    """Runs after the review is out (and its sha recorded): the report is
    translated on the main tier — it is long, Haiku leaves it half-English."""

    def __init__(self, settings: Settings, telegram: Any, translator: Translator) -> None:
        self.settings = settings
        self.telegram = telegram
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
                ref, msg(TESTER_REPORT_COMMENT, self.settings.pipeline.language, link=link))
        else:  # upload failed — inline the report so it isn't lost
            await ctx.vcs.post_note(ref, report[:60_000])

        caption = (f"🧪 Tester report: {ref.project_path} "
                   f"!{ref.mr_iid}\n{ref.url}")
        for chat_id in tester_report_targets(self.settings):
            await self.telegram.send_document(
                chat_id, filename, report.encode("utf-8"), caption)
        return ctx
