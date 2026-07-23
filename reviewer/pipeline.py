"""Review pipeline orchestrator.

Stages (PIPELINE_V2=on):
  0 context -> 1 triage (fast) -> 2 review (main; trivial -> fast)
  -> 3/4 investigate (smart agent loop; flag + triage-gated)
  -> 5 translate (EN->RU) -> 6 deliver (GitLab note, report upload, bridge doc, TG)

PIPELINE_V2=off: v1-parity single review (one main-tier call, output in
REVIEW_LANGUAGE directly), same user-facing strings as v1.
AI_PROVIDER=gemini: legacy gemini-wrapper.sh subprocess path (rollback hatch).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from typing import Any

import gitlab as gitlab_lib

from . import gitlab_io, prompts, review_state, telegram_io, usage
from .ai_client import AIClient, AIError, AIInputTooLargeError, AITimeoutError, ToolDef, ai_client
from .bridge import bridge
from .config import settings
from .repo_cache import repo_cache, repo_grep, repo_list_tree, repo_read_file

logger = logging.getLogger(__name__)

CONFLICT_SKIP_MSG = {
    "en": "⚠️ Merge request has conflicts. Code review skipped until conflicts are resolved.",
    "ru": "⚠️ Запрос на слияние имеет конфликты. Обзор кода пропущен до разрешения конфликтов.",
}
INITIAL_MSG = {
    "en": "🤖 Starting automated code review...",
    "ru": "🤖 Начинаем автоматический обзор кода...",
}
INITIAL_MSG_CONFLICT = {
    "en": "⚠️ 🤖 Starting automated code review (conflicts detected)...",
    "ru": "⚠️ 🤖 Начинаем автоматический обзор кода (обнаружены конфликты)...",
}
NO_CHANGES_MSG = {
    "en": "⚠️ No code changes found to review.",
    "ru": "⚠️ Не найдено изменений кода для обзора.",
}
TOO_LARGE_MSG = {
    "en": "⚠️ The merge request is too large to analyze. Please break it into smaller changes.",
    "ru": "⚠️ Запрос на слияние слишком большой для анализа. Пожалуйста, разбейте его на меньшие изменения.",
}
TIMEOUT_MSG = {
    "en": "⏱️ Code review timed out. The changes might be too large to analyze.",
    "ru": "⏱️ Тайм-аут обзора кода. Возможно, изменения слишком большие для анализа.",
}
FAILED_MSG = {
    "en": "❌ Code review failed: {error}",
    "ru": "❌ Обзор кода не удался: {error}",
}
GENERAL_ERROR_MSG = {
    "en": "❌ An error occurred during code review: {error}",
    "ru": "❌ Произошла ошибка при обзоре кода: {error}",
}
TESTER_REPORT_COMMENT = {
    "en": "## 🧪 Tester Report\n\nA verification guide for this MR is attached: {link}",
    "ru": "## 🧪 Отчёт для тестировщика\n\nИнструкция по проверке этого MR во вложении: {link}",
}


def _msg(table: dict[str, str], **kwargs) -> str:
    template = table.get(settings.review_language, table["en"])
    return template.format(**kwargs) if kwargs else template


def tester_report_targets() -> list[str]:
    """Chats that receive the tester-report document.

    Bridge chat first (AIManager archives reports into its corpus), then the
    team channels where the testers actually are (TESTER_REPORT_CHAT_IDS, or
    all regular notification channels when unset)."""
    targets: list[str] = []
    if settings.bridge_chat_id:
        targets.append(settings.bridge_chat_id)
    if settings.telegram_enabled:
        for chat_id in settings.tester_report_chat_ids or settings.telegram_chat_ids:
            if chat_id not in targets:
                targets.append(chat_id)
    return targets


def split_investigation(text: str) -> tuple[str, str | None]:
    """Split investigator output into (impact analysis, optional tester report)."""
    report = None
    impact = text
    if "## TESTER REPORT" in text:
        impact, rest = text.split("## TESTER REPORT", 1)
        report = "## TESTER REPORT\n\n" + rest.strip()
    return impact.strip(), report


class Pipeline:
    def __init__(self, client: AIClient | None = None):
        self.ai = client or ai_client

    # --- entry point ---

    async def process(self, mr_data: dict[str, Any]) -> None:
        gitlab_config = mr_data.get("gitlab_config")
        if not gitlab_config:
            logger.error("No GitLab configuration found in mr_data")
            return
        ctx = {"project_id": mr_data.get("project_id"), "mr_iid": mr_data.get("mr_iid"),
               "gitlab_instance": gitlab_config.get("name", "unknown")}
        tracker = usage.UsageTracker()
        tracker_token = usage.current_tracker.set(tracker)
        try:
            await self._process_inner(mr_data, gitlab_config, ctx)
        except gitlab_lib.exceptions.GitlabError as exc:
            logger.error("GitLab API error: %s", exc)
            await telegram_io.notify_error("gitlab_api_error", str(exc), ctx)
        except AIInputTooLargeError:
            await telegram_io.notify_error("ai_failure", "MR too large to analyze", ctx)
            await self._safe_note(mr_data, gitlab_config, _msg(TOO_LARGE_MSG))
        except AITimeoutError:
            logger.error("AI analysis timed out")
            await telegram_io.notify_error("timeout", "AI analysis exceeded timeout limit", ctx)
            await self._safe_note(mr_data, gitlab_config, _msg(TIMEOUT_MSG))
        except AIError as exc:
            logger.error("AI analysis failed: %s", exc)
            await telegram_io.notify_error("ai_failure", str(exc)[:300], ctx)
            await self._safe_note(mr_data, gitlab_config,
                                  _msg(FAILED_MSG, error=str(exc)[:300]))
        except Exception as exc:  # noqa: BLE001 — top-level pipeline guard
            logger.exception("Error in quality check")
            await telegram_io.notify_error("general", str(exc), ctx)
            await self._safe_note(mr_data, gitlab_config,
                                  _msg(GENERAL_ERROR_MSG, error=str(exc)))
        finally:
            usage.current_tracker.reset(tracker_token)
            usage.persist(tracker, mr_data)

    async def _safe_note(self, mr_data: dict, gitlab_config: dict, body: str) -> None:
        """Best-effort MR comment on error paths (v1 behavior)."""
        try:
            gl = await asyncio.to_thread(gitlab_io.get_gitlab_client, gitlab_config)
            project = await asyncio.to_thread(gl.projects.get, mr_data["project_id"])
            mr = await asyncio.to_thread(project.mergerequests.get, mr_data["mr_iid"])
            await gitlab_io.post_note(mr, body)
        except Exception:  # noqa: BLE001
            logger.error("Failed to post error message to MR")

    # --- main flow ---

    async def _process_inner(self, mr_data: dict, gitlab_config: dict, ctx: dict) -> None:
        logger.info("Starting quality check for MR !%s in project %s on %s",
                    mr_data["mr_iid"], mr_data["project_id"], gitlab_config["name"])

        gl = await asyncio.to_thread(gitlab_io.get_gitlab_client, gitlab_config)
        try:
            project = await asyncio.to_thread(gl.projects.get, mr_data["project_id"])
        except gitlab_lib.exceptions.GitlabGetError as exc:
            await telegram_io.notify_error(
                "gitlab_api_error", f"Failed to get project {mr_data['project_id']}: {exc}", ctx)
            return
        try:
            mr = await asyncio.to_thread(project.mergerequests.get, mr_data["mr_iid"])
        except gitlab_lib.exceptions.GitlabGetError as exc:
            await telegram_io.notify_error(
                "gitlab_api_error", f"Failed to get MR !{mr_data['mr_iid']}: {exc}", ctx)
            return

        # the webhook only queues open/update/reopen, but the MR can get merged
        # or closed while the event waits in the queue — don't burn tokens
        # reviewing an MR nobody can act on
        state = getattr(mr, "state", "opened")
        if state != "opened":
            logger.info("Skipping MR !%s: state is %s", mr_data["mr_iid"], state)
            return

        # incremental re-review: if we already reviewed this MR at some sha,
        # narrow this run to the delta since then — full re-reviews rehashed
        # remarks about earlier commits on every push (dev feedback 2026-07-23)
        head_sha = mr_data.get("last_commit") or getattr(mr, "sha", "") or ""
        prev_sha = review_state.get_last_sha(
            gitlab_config["name"], mr_data["project_id"], mr_data["mr_iid"])
        if prev_sha and head_sha and prev_sha == head_sha:
            logger.info("MR !%s already reviewed at %s — skipping (metadata-only "
                        "update)", mr_data["mr_iid"], head_sha[:8])
            return

        has_conflicts = await asyncio.to_thread(gitlab_io.check_merge_conflicts, mr)
        await telegram_io.notify(telegram_io.format_mr_message(
            mr_data, project.path_with_namespace, has_conflicts,
            gitlab_instance=gitlab_config["url"]))

        if has_conflicts and not settings.review_for_conflict:
            await gitlab_io.post_note(mr, _msg(CONFLICT_SKIP_MSG))
            logger.info("Skipped review for MR !%s due to conflicts", mr_data["mr_iid"])
            return

        incremental = False
        delta = None
        if prev_sha and head_sha:
            delta = await asyncio.to_thread(
                gitlab_io.fetch_delta_changes, project, prev_sha, head_sha)

        await gitlab_io.post_note(
            mr, _msg(INITIAL_MSG_CONFLICT if has_conflicts else INITIAL_MSG))

        # access_raw_diffs bypasses GitLab's per-file collapse limit, which
        # otherwise returns empty diffs for large files (silently unreviewed)
        if delta:
            changes = delta
            incremental = True
            logger.info("incremental re-review for MR !%s: %s..%s (%d files)",
                        mr_data["mr_iid"], prev_sha[:8], head_sha[:8],
                        len(delta["changes"]))
        else:
            changes = await asyncio.to_thread(
                lambda: mr.changes(access_raw_diffs="true"))
        review_content = await asyncio.to_thread(
            gitlab_io.extract_review_content, project, mr, changes)
        if not review_content:
            await gitlab_io.post_note(mr, _msg(NO_CHANGES_MSG))
            return

        diff_only = gitlab_io.extract_diff_only(changes)

        def _mark_reviewed(posted: bool) -> None:
            if posted:
                review_state.set_last_sha(gitlab_config["name"], mr_data["project_id"],
                                          mr_data["mr_iid"], head_sha)

        if settings.ai_provider == "gemini":
            review_ru = await self._legacy_gemini_review(mr_data, review_content)
            _mark_reviewed(await self._deliver_review(
                mr, mr_data, project, gitlab_config, has_conflicts, review_ru))
            return

        if not settings.pipeline_v2:
            review_text = await self._parity_review(mr_data, review_content, diff_only)
            _mark_reviewed(await self._deliver_review(
                mr, mr_data, project, gitlab_config, has_conflicts, review_text))
            return

        # ---- tiered pipeline ----
        # per-project reviewer config (.ai-review.md) + incremental focus
        guidelines = await asyncio.to_thread(
            gitlab_io.fetch_review_guidelines, project,
            mr_data.get("target_branch") or getattr(project, "default_branch", ""))
        system_extra = ""
        if guidelines:
            system_extra += prompts.guidelines_section(guidelines)
        if incremental:
            system_extra += prompts.INCREMENTAL_REVIEW_NOTE.format(
                prev_sha=(prev_sha or "")[:8])

        triage = await self._triage(mr_data, changes)
        review_en = await self._review(mr_data, review_content, triage, diff_only,
                                       system_extra)

        investigation = None
        if (settings.investigator and triage.get("needs_investigation")
                and triage.get("complexity") == "complex"):
            investigation = await self._investigate(
                mr_data, gitlab_config, review_content, triage, review_en)
            if investigation and investigation.get("impact"):
                # the impact analysis belongs in the review comment — only the
                # tester report is gated behind TESTER_REPORT below
                review_en += "\n\n---\n\n" + investigation["impact"]

        review_out = await self._translate_if_needed(review_en, tier="fast")
        _mark_reviewed(await self._deliver_review(
            mr, mr_data, project, gitlab_config, has_conflicts, review_out))

        if investigation and settings.tester_report and investigation.get("tester_report"):
            report_ru = await self._translate_if_needed(
                investigation["tester_report"], tier="main")
            await self._deliver_tester_report(project, mr, mr_data, report_ru)

    # --- stages ---

    async def _triage(self, mr_data: dict, changes: dict) -> dict:
        diff_summary = gitlab_io.extract_diff_only(changes)[:60_000]
        fallback = {"complexity": "normal", "risk_areas": [],
                    "jira_keys": gitlab_io.extract_jira_keys(mr_data),
                    "needs_investigation": False, "summary": mr_data.get("title", "")}
        try:
            parsed = await self.ai.complete_json(
                "fast", prompts.TRIAGE_SYSTEM,
                prompts.triage_user_prompt(mr_data, diff_summary),
                prompts.TRIAGE_SCHEMA)
        except AIError as exc:
            logger.warning("triage failed (%s) — defaulting to normal", exc)
            return fallback
        if not parsed or "complexity" not in parsed:
            logger.warning("triage returned unparseable output — defaulting to normal")
            return fallback
        # merge regex-found keys the model may have missed (lenient fallback parse
        # may return a non-list here — normalize instead of crashing the review)
        if not isinstance(parsed.get("jira_keys"), list):
            parsed["jira_keys"] = []
        for key in fallback["jira_keys"]:
            if key not in parsed["jira_keys"]:
                parsed["jira_keys"].append(key)
        logger.info("triage: complexity=%s investigate=%s jira=%s",
                    parsed.get("complexity"), parsed.get("needs_investigation"),
                    parsed.get("jira_keys"))
        return parsed

    async def _review(self, mr_data: dict, review_content: str, triage: dict,
                      diff_only: str = "", system_extra: str = "") -> str:
        if triage.get("complexity") == "trivial":
            user = prompts.review_user_prompt(gitlab_io.mr_header(mr_data), review_content)
            result = await self.ai.complete(
                "fast", prompts.TRIVIAL_REVIEW_SYSTEM + system_extra, user,
                max_tokens=1024)
            return result.text
        system = (settings.review_prompt_en or prompts.REVIEW_SYSTEM) + system_extra
        if triage.get("risk_areas"):
            system += "\nTriage flagged risk areas: " + ", ".join(triage["risk_areas"])
        result = await self._complete_with_degradation(
            "main", system, mr_data, review_content, diff_only, effort="high")
        return result.text

    async def _parity_review(self, mr_data: dict, review_content: str,
                             diff_only: str = "") -> str:
        """PIPELINE_V2=off: one main-tier call writing directly in REVIEW_LANGUAGE (v1 shape)."""
        if settings.review_language == "ru":
            system = settings.review_prompt_ru or (
                prompts.REVIEW_SYSTEM.replace("Write in English.", "Пиши по-русски."))
        else:
            system = settings.review_prompt_en or prompts.REVIEW_SYSTEM
        result = await self._complete_with_degradation(
            "main", system, mr_data, review_content, diff_only)
        return result.text

    async def _complete_with_degradation(self, tier: str, system: str, mr_data: dict,
                                         review_content: str, diff_only: str,
                                         effort: str | None = None):
        """Review with file context; if too large, retry diff-only before refusing."""
        try:
            user = prompts.review_user_prompt(gitlab_io.mr_header(mr_data), review_content)
            return await self.ai.complete(tier, system, user, max_tokens=16000, effort=effort)
        except AIInputTooLargeError:
            if not diff_only:
                raise
            logger.warning("MR !%s too large with file context — retrying diff-only",
                           mr_data["mr_iid"])
            user = prompts.review_user_prompt(
                gitlab_io.mr_header(mr_data)
                + "\n(file context omitted — MR too large; diffs only)", diff_only)
            return await self.ai.complete(tier, system, user, max_tokens=16000, effort=effort)

    async def _investigate(self, mr_data: dict, gitlab_config: dict,
                           review_content: str, triage: dict, review_en: str) -> dict | None:
        worktree = None
        try:
            worktree = await repo_cache.checkout_mr(
                gitlab_config, mr_data["project_path"], mr_data["mr_iid"],
                mr_data.get("last_commit"))
        except Exception as exc:  # noqa: BLE001 — investigation degrades, review still ships
            logger.error("repo checkout failed, investigating without repo tools: %s", exc)

        tools: list[ToolDef] = []
        if worktree is not None:
            wt = worktree
            tools += [
                ToolDef("repo_grep",
                        "Search the project for a regex pattern. Returns file:line: text matches.",
                        {"type": "object", "properties": {
                            "pattern": {"type": "string", "description": "Python regex"},
                            "glob": {"type": "string", "description": "optional path glob, e.g. **/*.py"},
                            "max_results": {"type": "integer"}},
                         "required": ["pattern"]},
                        handler=lambda **kw: asyncio.to_thread(repo_grep, wt, **kw)),
                ToolDef("repo_read_file",
                        "Read a file from the project at the MR head commit (line-numbered).",
                        {"type": "object", "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"}},
                         "required": ["path"]},
                        handler=lambda **kw: asyncio.to_thread(repo_read_file, wt, **kw)),
                ToolDef("repo_list_tree",
                        "List files/directories under a path.",
                        {"type": "object", "properties": {
                            "path": {"type": "string"}, "depth": {"type": "integer"}},
                         "required": []},
                        handler=lambda **kw: asyncio.to_thread(repo_list_tree, wt, **kw)),
            ]

        questions_left = settings.bridge_max_questions_per_mr

        async def ask_aimanager(question: str) -> str:
            nonlocal questions_left
            if not bridge.enabled:
                return "AIManager is not available in this deployment."
            if questions_left <= 0:
                return "Question budget for this MR is exhausted."
            questions_left -= 1
            answer = await bridge.ask(question)
            return answer or "No answer received (timeout or not found)."

        if bridge.enabled:
            tools.append(ToolDef(
                "ask_aimanager",
                "Ask the company knowledge bot (Jira corpus + project chats) one focused "
                "plain-text question. Include the Jira issue key when known. 15-60s latency. "
                + prompts.BRIDGE_QUESTION_HINT.format(key="<KEY>"),
                {"type": "object", "properties": {"question": {"type": "string"}},
                 "required": ["question"]},
                handler=ask_aimanager))

        system = prompts.INVESTIGATOR_SYSTEM.format(
            max_iterations=settings.investigator_max_iterations)
        user = prompts.investigator_user_prompt(mr_data, review_content, triage, review_en)
        try:
            result = await self.ai.agent_loop(
                "smart", system, user, tools,
                max_iterations=settings.investigator_max_iterations,
                # 32k: adaptive thinking bills against max_tokens on the smart
                # tier — 16k could be consumed before any visible text
                max_tokens=32000, effort="high")
        except AIError as exc:
            logger.error("investigation failed: %s", exc)
            return None
        finally:
            if worktree is not None:
                await repo_cache.release(worktree)

        impact, report = split_investigation(result.text)
        logger.info("investigation done: %d chars, tester_report=%s, tokens in=%d out=%d",
                    len(result.text), bool(report), result.input_tokens, result.output_tokens)
        return {"full_text": result.text, "impact": impact, "tester_report": report}

    async def _translate_if_needed(self, text: str, tier: str) -> str:
        if settings.review_language != "ru" or not text:
            return text
        # long documents overwhelm Haiku: it starts leaving half the prose in
        # English mid-sentence (dev feedback 2026-07-23) — main tier handles them
        if tier == "fast" and len(text) > 3500:
            tier = "main"
        try:
            result = await self.ai.complete(
                tier, prompts.TRANSLATE_SYSTEM, prompts.translate_user_prompt(text),
                max_tokens=max(2048, min(16000, len(text))), use_cache=True)
        except AIError as exc:
            logger.error("translation failed, delivering English original: %s", exc)
            return text
        out = (result.text or "").strip()
        # invariant: a RU translation contains Cyrillic; anything else is model
        # commentary (e.g. Haiku asking for "the document") — deliver the original
        if out and any("Ѐ" <= ch <= "ӿ" for ch in out):
            return out
        logger.warning("translation output had no Cyrillic — delivering English original")
        return text

    # --- delivery ---

    async def _deliver_review(self, mr, mr_data: dict, project, gitlab_config: dict,
                              has_conflicts: bool, review_text: str) -> bool:
        """Returns True when the review comment was posted (drives the
        last-reviewed-sha state for incremental re-reviews)."""
        comment = gitlab_io.format_review_comment(review_text)
        try:
            await gitlab_io.post_note(mr, comment)
            logger.info("Posted review for MR !%s", mr_data["mr_iid"])
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to post review comment: %s", exc)
            await telegram_io.notify_error(
                "gitlab_api_error", f"Failed to post review comment: {exc}",
                {"project_id": mr_data["project_id"], "mr_iid": mr_data["mr_iid"],
                 "gitlab_instance": gitlab_config["name"]})
            error_msg = {
                "en": f"❌ Failed to post review comment: {exc}",
                "ru": f"❌ Не удалось опубликовать комментарий с обзором: {exc}",
            }
            try:
                await gitlab_io.post_note(mr, _msg(error_msg))
            except Exception:  # noqa: BLE001
                logger.error("Failed to post error message as well")
            return False
        if settings.telegram_enabled:
            await telegram_io.notify(telegram_io.format_mr_message(
                mr_data, project.path_with_namespace, has_conflicts,
                review_text, gitlab_config["url"]))
        return True

    async def _deliver_tester_report(self, project, mr, mr_data: dict,
                                     report_ru: str) -> None:
        filename = (f"tester-report-{mr_data['project_path'].replace('/', '-')}"
                    f"-MR{mr_data['mr_iid']}.md")
        link = await gitlab_io.upload_tester_report(project, filename, report_ru)
        if link:
            await gitlab_io.post_note(mr, _msg(TESTER_REPORT_COMMENT, link=link))
        else:  # upload failed — inline the report so it isn't lost
            await gitlab_io.post_note(mr, report_ru[:60_000])

        caption = (f"🧪 Tester report: {mr_data['project_path']} "
                   f"!{mr_data['mr_iid']}\n{mr_data['url']}")
        for chat_id in tester_report_targets():
            await telegram_io.send_document(
                chat_id, filename, report_ru.encode("utf-8"), caption)

    # --- legacy gemini path (rollback hatch) ---

    async def _legacy_gemini_review(self, mr_data: dict, review_content: str) -> str:
        payload = f"{gitlab_io.mr_header(mr_data)}\n\n{review_content}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                         encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        try:
            result = await asyncio.to_thread(
                subprocess.run, ["./gemini-wrapper.sh", tmp_path],
                capture_output=True, text=True, timeout=120,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                encoding="utf-8", errors="replace")
            if result.returncode != 0:
                raise AIError(f"gemini-wrapper exit {result.returncode}: {result.stderr[:300]}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired as exc:
            raise AITimeoutError("gemini-wrapper timed out") from exc
        finally:
            os.unlink(tmp_path)


pipeline = Pipeline()
