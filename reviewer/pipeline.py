"""Review pipeline facade (refactoring stage 13).

The MR review itself is the `application.review_mr.ReviewMergeRequest` use
case over the stages in `application/stages/`; this class still hosts the MR
discussion dialogue until stage 14 moves it out.
AI_PROVIDER=openrouter: every tier goes to OpenRouter, not only after a gateway failure.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace

from . import prompts, usage
from .ai_client import AIClient
from .application import content
from .application.common import bot_username, new_job_id
from .application.ports import RepoWorkspace, VcsPort
from .application.review_mr import ReviewMergeRequest
from .application.stages import Translator, tester_report_targets
from .bridge import ReviewBridge
from .config import Settings
from .domain.models import DialogueJob, InstanceRef, ReviewJob, Tier
from .review_state import ReviewStateStore
from .telegram_io import TelegramClient
from .usage import UsageLog

logger = logging.getLogger(__name__)

__all__ = ["Pipeline", "new_job_id", "tester_report_targets"]

# dialogue replies get the diff as reference, capped — repo tools cover the rest
DIALOGUE_DIFF_MAX_CHARS = 60_000
DIALOGUE_WINDOW_SECONDS = 86_400  # per-MR reply budget window


class Pipeline:
    def __init__(self, settings: Settings, *, ai: AIClient, telegram: TelegramClient,
                 bridge: ReviewBridge, workspace: RepoWorkspace,
                 review_state: ReviewStateStore, usage_log: UsageLog,
                 vcs: Callable[[InstanceRef], VcsPort],
                 pricing: usage.Pricing = usage.BUILTIN_PRICING) -> None:
        self.settings = settings
        self.ai = ai
        self.telegram = telegram
        self.workspace = workspace
        self.usage_log = usage_log
        self.pricing = pricing
        # instance -> its VCS client (one per instance, built by bootstrap)
        self.vcs = vcs
        self.translator = Translator(ai, settings.pipeline.language)
        self.review_mr = ReviewMergeRequest(
            settings, ai=ai, telegram=telegram, bridge=bridge, workspace=workspace,
            review_state=review_state, usage_log=usage_log, vcs=vcs,
            translator=self.translator, pricing=pricing)
        # (instance, project_id, mr_iid) -> timestamps of dialogue replies sent
        self._dialogue_replies: dict[tuple, list[float]] = {}

    async def process(self, job: ReviewJob) -> None:
        await self.review_mr.execute(job)

    # --- MR discussion dialogue ---

    async def process_note(self, job: DialogueJob) -> None:
        """Answer a developer's reply in an MR discussion thread ("Пусть сам
        подтверждает" — dev feedback 2026-07-31: instead of the reviewer asking
        humans to confirm things, humans can now ask IT, and it checks the repo)."""
        if not job.job_id:
            job = replace(job, job_id=new_job_id())
        logger.info("job %s: dialogue for note %s in MR !%s", job.job_id,
                    job.note_id, job.ref.mr_iid)
        tracker = usage.UsageTracker(self.pricing)
        tracker_token = usage.current_tracker.set(tracker)
        try:
            await self._process_note_inner(job)
        except Exception:  # noqa: BLE001 — a failed reply must not spam the thread
            logger.exception("job %s: dialogue failed for note %s in MR !%s", job.job_id,
                             job.note_id, job.ref.mr_iid)
        finally:
            usage.current_tracker.reset(tracker_token)
            self.usage_log.persist(tracker, job)

    async def _process_note_inner(self, job: DialogueJob) -> None:
        ref, note_id = job.ref, job.note_id
        mr_iid = ref.mr_iid
        vcs = self.vcs(ref.instance)
        bot = await bot_username(vcs)
        author = job.note_author
        if bot and author == bot:
            return  # our own review/reply notes fire note hooks too
        mr = await vcs.get_merge_request(ref)

        discussion = await vcs.find_discussion(ref, note_id, job.discussion_id)
        discussion_id, notes = (discussion.id, discussion.notes) if discussion else ("", ())
        mentioned = content.mentions_user(job.note_body, bot)
        # only answer inside threads the bot is part of, or on an explicit
        # @mention — everything else is the humans talking to each other
        if not (mentioned or content.thread_involves_bot(notes, bot)):
            logger.debug("note %s: not our thread and no mention — ignoring", note_id)
            return
        if content.bot_answered_after(notes, note_id, bot):
            logger.info("note %s: already answered — skipping", note_id)
            return
        if not self._dialogue_budget_ok(ref.key):
            logger.warning("dialogue reply budget exhausted for MR !%s — staying "
                           "silent", mr_iid)
            return

        logger.info("dialogue: answering @%s in %s!%s", author, ref.project_path, mr_iid)
        changes = await vcs.get_changes(ref)
        diff = content.extract_diff_only(changes, max_chars=DIALOGUE_DIFF_MAX_CHARS)
        thread_text = (content.render_thread(notes, bot) if notes
                       else f"[@{author}]:\n{job.note_body}")
        header = content.mr_header(mr.title, mr.author or author,
                                   mr.source_branch, mr.target_branch)

        user = prompts.dialogue_user_prompt(
            header, thread_text, author, job.note_position, diff)
        async with self.workspace.session(ref, job.last_commit or mr.sha or None) as tools:
            result = await self.ai.agent_loop(
                Tier.MAIN, prompts.DIALOGUE_SYSTEM, user, tools or [],
                max_iterations=self.settings.pipeline.review_max_tool_calls + 2,
                max_tokens=4000)

        text = (result.text or "").strip()
        if not text or text.upper().startswith("NO_REPLY"):
            logger.info("dialogue: nothing to answer in note %s", note_id)
            return
        reply = await self.translator.translate(text, tier=Tier.FAST)

        posted = False
        if discussion_id:
            try:
                await vcs.reply_in_discussion(ref, discussion_id, reply)
                posted = True
            except Exception as exc:  # noqa: BLE001 — thread reply can 400 on odd notes
                logger.warning("discussion reply failed (%s) — posting a plain note", exc)
        if not posted:
            quote = "\n".join("> " + line for line in job.note_body.splitlines()[:6])
            await vcs.post_note(ref, f"@{author}\n\n{quote}\n\n{reply}")
        self._dialogue_replied(ref.key)
        logger.info("dialogue: replied in MR !%s (thread %s)", mr_iid,
                    discussion_id or "new")

    def _dialogue_budget_ok(self, key: tuple) -> bool:
        now = time.time()
        stamps = [t for t in self._dialogue_replies.get(key, ())
                  if now - t < DIALOGUE_WINDOW_SECONDS]
        self._dialogue_replies[key] = stamps
        if len(self._dialogue_replies) > 500:  # bound the map itself
            self._dialogue_replies = {
                k: v for k, v in self._dialogue_replies.items()
                if v and now - v[-1] < DIALOGUE_WINDOW_SECONDS}
        return len(stamps) < self.settings.pipeline.dialogue_max_replies_per_mr

    def _dialogue_replied(self, key: tuple) -> None:
        self._dialogue_replies.setdefault(key, []).append(time.time())
