"""Notification events -> Telegram Markdown (the v1 message format, verbatim)."""

from __future__ import annotations

from datetime import datetime

from ....domain.events import (
    MrSummary,
    ReviewFailed,
    SystemAlert,
    TesterReportReady,
    UsageSummary,
)
from ....i18n import has, t

# the inline review is a preview; a longer one only links to GitLab
REVIEW_INLINE_MAX = 2000
REVIEW_PREVIEW_CHARS = 1500

# v1 called it gemini_failure; same alert, same title
ERROR_ALIASES = {"gemini_failure": "ai_failure"}


def usage_footer(usage: UsageSummary) -> str:
    """Compact usage footer (AIManager style):
    `haiku-4-5: →19448 ←446 | sonnet-5: →104634 ←7457 | 💰$0.63`."""
    segs = []
    for m in sorted(usage.models, key=lambda m: m.model):
        name = m.model.split("/")[-1].replace("claude-", "")
        segs.append(f"{name}: →{m.input_tokens} ←{m.output_tokens}")
    segs.append(f"💰${usage.total_cost_usd:.2f}")
    return " | ".join(segs)


class TelegramFormatter:
    def __init__(self, language: str = "en") -> None:
        self.language = language

    def _lang(self, event_language: str) -> str:
        return event_language or self.language

    def mr_message(self, mr: MrSummary, has_conflicts: bool = False,
                   review_text: str | None = None, usage: UsageSummary | None = None,
                   language: str = "") -> str:
        lang = self._lang(language)
        if has_conflicts:
            status_emoji, status_text = "⚠️", t("telegram.mr_conflicts", lang)
        else:
            status_emoji, status_text = "✅", t("telegram.mr_new", lang)

        instance_info = ""
        if mr.instance_url:
            domain = mr.instance_url.split("://")[-1].rstrip("/")
            instance_info = f" from `{domain}`"

        parts = [
            f"{status_emoji} **{status_text}{instance_info}**",
            f"**Project:** `{mr.project_path}`",
            f"**Author:** {mr.author}",
            f"**Title:** {mr.title}",
            f"**Branch:** `{mr.source_branch}` → `{mr.target_branch}`",
            f"**Link:** [!{mr.mr_iid}]({mr.url})",
        ]
        if has_conflicts:
            parts.append("")
            parts.append(t("telegram.mr_blocked", lang))

        if review_text and len(review_text) < REVIEW_INLINE_MAX:
            parts.append(t("telegram.review_heading", lang))
            truncated = (review_text[:REVIEW_PREVIEW_CHARS] + "..."
                         if len(review_text) > REVIEW_PREVIEW_CHARS else review_text)
            parts.append(f"```\n{truncated}\n```")
        elif review_text:
            parts.append(t("telegram.review_too_long", lang))

        # usage footer only on review-completion messages
        if review_text and usage is not None and usage.models:
            parts.append(f"\n`{usage_footer(usage)}`")
        return "\n".join(parts)

    def error_message(self, event: ReviewFailed | SystemAlert) -> str:
        lang = self._lang(event.language)
        kind = ERROR_ALIASES.get(event.kind, event.kind)
        title_key = f"telegram.error.{kind}"
        if not has(title_key):
            title_key = "telegram.error.general"
        parts = [f"**{t(title_key, lang)}**", f"**Details:** {event.details}"]
        if isinstance(event, ReviewFailed):
            mr = event.mr
            project_id, mr_iid, instance = ((mr.project_id, mr.mr_iid, mr.instance)
                                            if mr else (None, None, ""))
        else:
            project_id, mr_iid, instance = event.project_id, event.mr_iid, event.instance
        if project_id is not None:
            parts.append(f"**Project ID:** {project_id}")
        if mr_iid is not None:
            parts.append(f"**MR:** !{mr_iid}")
        if instance:
            parts.append(f"**Instance:** {instance}")
        if event.job_id:
            parts.append(f"**Job:** {event.job_id}")
        parts.append(f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return t("telegram.error_prefix", lang) + "\n".join(parts)

    @staticmethod
    def tester_report_caption(event: TesterReportReady) -> str:
        return f"🧪 Tester report: {event.mr.project_path} !{event.mr.mr_iid}\n{event.mr.url}"
