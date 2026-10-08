"""Use case: answer a developer's note in an MR discussion thread.

"Пусть сам подтверждает" (dev feedback 2026-07-31): instead of the reviewer
asking humans to confirm things, humans can ask IT, and it checks the repo.
Guards, in order: the bot's own note, a thread the bot is not part of (and no
@mention), an already-answered note, the per-MR daily reply budget. The model
may decline with NO_REPLY (acks, thanks). A failed reply is never posted — a
broken answer must not spam the thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from .. import prompts, usage
from ..config import Settings
from ..domain.models import DialogueJob, InstanceRef, Tier
from ..logging_setup import job_context
from ..prompts import Prompts, default_prompts
from ..usage import UsageLog
from . import content
from .common import bot_username, new_job_id
from .ports import RepoWorkspace, VcsPort
from .stages import Translator

logger = logging.getLogger(__name__)

# dialogue replies get the diff as reference, capped — repo tools cover the rest
DIALOGUE_DIFF_MAX_CHARS = 60_000
NO_REPLY = "NO_REPLY"


class ReplyBudget(Protocol):
    def allows(self, mr_key: tuple) -> bool: ...

    def record(self, mr_key: tuple) -> None: ...


class AnswerNote:
    def __init__(self, settings: Settings, *, ai: Any, workspace: RepoWorkspace,
                 usage_log: UsageLog, vcs: Callable[[InstanceRef], VcsPort],
                 translator: Translator, budget: ReplyBudget,
                 pricing: usage.Pricing = usage.BUILTIN_PRICING,
                 templates: Prompts | None = None) -> None:
        self.settings = settings
        self.ai = ai
        self.workspace = workspace
        self.usage_log = usage_log
        self.vcs = vcs
        self.translator = translator
        self.budget = budget
        self.pricing = pricing
        self.templates = templates or default_prompts()

    async def execute(self, job: DialogueJob) -> None:
        if not job.job_id:
            job = replace(job, job_id=new_job_id())
        logger.info("job %s: dialogue for note %s in MR !%s", job.job_id,
                    job.note_id, job.ref.mr_iid)
        tracker = usage.UsageTracker(self.pricing)
        with job_context(job, usage=tracker):
            try:
                await self.run(job)
            except Exception:  # noqa: BLE001 — a failed reply must not spam the thread
                logger.exception("job %s: dialogue failed for note %s in MR !%s",
                                 job.job_id, job.note_id, job.ref.mr_iid)
            finally:
                self.usage_log.persist(tracker, job)

    async def run(self, job: DialogueJob) -> None:
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
        if not self.budget.allows(ref.key):
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
                Tier.MAIN, self.templates.DIALOGUE_SYSTEM, user, tools or [],
                max_iterations=self.settings.pipeline.review_max_tool_calls + 2,
                max_tokens=4000)

        text = (result.text or "").strip()
        if not text or text.upper().startswith(NO_REPLY):
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
        self.budget.record(ref.key)
        logger.info("dialogue: replied in MR !%s (thread %s)", mr_iid,
                    discussion_id or "new")
