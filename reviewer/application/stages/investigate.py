"""Stages 3/4 — investigator (smart tier agent loop): whole-repo analysis
with the session's repo tools plus AIManager questions over the bridge;
complex MRs only. Its impact analysis joins the review comment."""

from __future__ import annotations

import logging
from typing import Any

from ... import prompts
from ...ai_client import CHARS_PER_TOKEN, AIError, AIInputTooLargeError, ToolDef
from ...config import Settings
from ...domain import budget
from ...domain.investigation import investigation_from_text
from ...domain.models import Investigation, ReviewJob, Tier, TriageResult
from .base import ReviewContext

logger = logging.getLogger(__name__)


class Investigate:
    def __init__(self, settings: Settings, ai: Any, bridge: Any) -> None:
        self.settings = settings
        self.ai = ai
        self.bridge = bridge

    async def run(self, ctx: ReviewContext) -> ReviewContext:
        ctx.investigation = await self.investigate(
            ctx.job, ctx.review_content, ctx.triage, ctx.review_en, ctx.diff_only,
            ctx.repo_tools)
        if ctx.investigation and ctx.investigation.impact:
            # the impact analysis belongs in the review comment — only the
            # tester report is gated behind TESTER_REPORT
            ctx.review_en += "\n\n---\n\n" + ctx.investigation.impact
        return ctx

    def _system(self) -> str:
        return prompts.INVESTIGATOR_SYSTEM.format(
            max_iterations=self.settings.pipeline.investigator_max_iterations)

    def content_for(self, job: ReviewJob, review_content: str,
                    triage: TriageResult, review_en: str, diff_only: str) -> str:
        """Pick the largest context that fits, BEFORE paying for a repo clone.

        The investigator gets the same content as the review, which on a big MR
        is exactly what the review's own size guard just rejected — checking
        after the clone means cloning a whole repo only to give up."""
        system = self._system()
        for candidate in (review_content, diff_only):
            if not candidate:
                continue
            try:
                self.ai.guard_input_size(
                    system,
                    prompts.investigator_user_prompt(job, candidate, triage, review_en))
                return candidate
            except AIInputTooLargeError:
                continue
        limit = budget.trim_budget_chars(self.settings.llm.max_input_tokens, CHARS_PER_TOKEN,
                                         budget.INVESTIGATOR_TRIM_SHARE)
        logger.warning("MR !%s: investigating on a %d-char diff subset",
                       job.ref.mr_iid, limit)
        return (diff_only or review_content)[:limit]

    def _bridge_tool(self) -> ToolDef:
        questions_left = self.settings.bridge.max_questions_per_mr

        async def ask_aimanager(question: str) -> str:
            nonlocal questions_left
            if not self.bridge.enabled:
                return "AIManager is not available in this deployment."
            if questions_left <= 0:
                return "Question budget for this MR is exhausted."
            questions_left -= 1
            answer = await self.bridge.ask(question)
            return answer or "No answer received (timeout or not found)."

        return ToolDef(
            "ask_aimanager",
            "Ask the company knowledge bot (Jira corpus + project chats) one focused "
            "plain-text question. Include the Jira issue key when known. 15-60s latency. "
            + prompts.BRIDGE_QUESTION_HINT.format(key="<KEY>"),
            {"type": "object", "properties": {"question": {"type": "string"}},
             "required": ["question"]},
            handler=ask_aimanager)

    async def investigate(self, job: ReviewJob, review_content: str, triage: TriageResult,
                          review_en: str, diff_only: str = "",
                          repo_tools: list[ToolDef] | None = None) -> Investigation | None:
        """Agentic whole-repo investigation. The caller owns the repo session
        (checked out once, shared with the tool-assisted review)."""
        review_content = self.content_for(job, review_content, triage, review_en, diff_only)
        tools: list[ToolDef] = list(repo_tools) if repo_tools is not None else []
        if repo_tools is None:
            logger.warning("investigating without repo tools (no checkout)")
        if self.bridge.enabled:
            tools.append(self._bridge_tool())

        user = prompts.investigator_user_prompt(job, review_content, triage, review_en)
        try:
            result = await self.ai.agent_loop(
                Tier.SMART, self._system(), user, tools,
                max_iterations=self.settings.pipeline.investigator_max_iterations,
                # 32k: adaptive thinking bills against max_tokens on the smart
                # tier — 16k could be consumed before any visible text
                max_tokens=32000, effort="high")
        except AIError as exc:
            logger.error("investigation failed: %s", exc)
            return None

        investigation = investigation_from_text(result.text)
        logger.info("investigation done: %d chars, tester_report=%s, tokens in=%d out=%d",
                    len(result.text), bool(investigation.tester_report),
                    result.input_tokens, result.output_tokens)
        return investigation
