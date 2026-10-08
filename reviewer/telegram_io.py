"""Telegram notifications (async httpx). Ported from v1; adds sendDocument.

Error-type taxonomy preserved; `ai_failure` succeeds v1's `gemini_failure`
(the old name is accepted as an alias).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from . import usage
from .config import TelegramSection
from .domain.models import ReviewJob
from .i18n import has, t

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"


# v1 called it gemini_failure; same alert, same title
_ERROR_ALIASES = {"gemini_failure": "ai_failure"}


class TelegramClient:
    """Bot API sender for notifications, error alerts and tester-report
    documents. `send_message` / `send_document` are the wire level — a test
    fake overrides just those, so formatting runs for real."""

    def __init__(self, cfg: TelegramSection, *, proxy_url: str | None = None,
                 language: str = "en") -> None:
        self.cfg = cfg
        self.proxy_url = proxy_url
        self.language = language

    def redact_token(self, text: str) -> str:
        """httpx exceptions embed the request URL, which contains the bot token."""
        if self.cfg.token:
            return text.replace(self.cfg.token, "***TOKEN***")
        return text


    def _client(self, timeout: float = 15.0) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return httpx.AsyncClient(**kwargs)


    async def send_message(self, chat_id: str, text: str, *, parse_mode: str | None = "Markdown") -> bool:
        if not self.cfg.token:
            return False
        data: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                "disable_web_page_preview": True}
        if parse_mode:
            data["parse_mode"] = parse_mode
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{_API}/bot{self.cfg.token}/sendMessage", json=data)
                if response.status_code != 200 and parse_mode:
                    # Markdown parse failures are common with code in MR titles — retry plain
                    data.pop("parse_mode", None)
                    response = await client.post(
                        f"{_API}/bot{self.cfg.token}/sendMessage", json=data)
                response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 — notifications must never break the pipeline
            logger.error("Telegram sendMessage to %s failed: %s", chat_id, self.redact_token(str(exc)))
            return False


    async def send_document(self, chat_id: str, filename: str, content: bytes, caption: str = "") -> bool:
        if not self.cfg.token:
            return False
        try:
            async with self._client(timeout=60.0) as client:
                response = await client.post(
                    f"{_API}/bot{self.cfg.token}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption[:1024]},
                    files={"document": (filename, content, "text/markdown")},
                )
                response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Telegram sendDocument to %s failed: %s", chat_id, self.redact_token(str(exc)))
            return False


    async def notify(self, message: str, is_error: bool = False) -> bool:
        """Broadcast to all configured notification channels (v1 behavior)."""
        if not (self.cfg.enabled and self.cfg.token and self.cfg.chat_ids):
            logger.debug("Telegram notifications disabled or not configured")
            return False
        if is_error:
            message = t("telegram.error_prefix", self.language) + message
        sent = 0
        for chat_id in self.cfg.chat_ids:
            if await self.send_message(chat_id, message):
                sent += 1
        return sent > 0


    async def notify_error(self, error_type: str, error_details: str,
                           context: dict[str, Any] | None = None) -> bool:
        if not self.cfg.enabled:
            return False
        kind = _ERROR_ALIASES.get(error_type, error_type)
        title_key = f"telegram.error.{kind}"
        if not has(title_key):
            title_key = "telegram.error.general"
        parts = [f"**{t(title_key, self.language)}**",
                 f"**Details:** {error_details}"]
        context = context or {}
        if "project_id" in context:
            parts.append(f"**Project ID:** {context['project_id']}")
        if "mr_iid" in context:
            parts.append(f"**MR:** !{context['mr_iid']}")
        if "gitlab_instance" in context:
            parts.append(f"**Instance:** {context['gitlab_instance']}")
        if "job_id" in context:
            parts.append(f"**Job:** {context['job_id']}")
        parts.append(f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return await self.notify("\n".join(parts), is_error=True)


    def format_mr_message(self, job: ReviewJob, project_name: str, has_conflicts: bool = False,
                          review_content: str | None = None,
                          gitlab_instance: str | None = None) -> str:
        """v1 notification format, preserved verbatim."""
        lang = self.language
        if has_conflicts:
            status_emoji = "⚠️"
            status_text = t("telegram.mr_conflicts", lang)
        else:
            status_emoji = "✅"
            status_text = t("telegram.mr_new", lang)

        instance_info = ""
        if gitlab_instance:
            domain = gitlab_instance.split("://")[-1].rstrip("/")
            instance_info = f" from `{domain}`"

        parts = [
            f"{status_emoji} **{status_text}{instance_info}**",
            f"**Project:** `{project_name}`",
            f"**Author:** {job.author}",
            f"**Title:** {job.title}",
            f"**Branch:** `{job.source_branch}` → `{job.target_branch}`",
            f"**Link:** [!{job.ref.mr_iid}]({job.ref.url})",
        ]

        if has_conflicts:
            parts.append("")
            parts.append(t("telegram.mr_blocked", lang))

        if review_content and len(review_content) < 2000:
            parts.append(t("telegram.review_heading", lang))
            truncated = review_content[:1500] + "..." if len(review_content) > 1500 else review_content
            parts.append(f"```\n{truncated}\n```")
        elif review_content:
            parts.append(t("telegram.review_too_long", lang))

        # usage footer (AIManager style) — only on review-completion messages
        if review_content:
            tracker = usage.current_tracker.get()
            if tracker is not None and tracker.calls:
                parts.append(f"\n`{tracker.footer_line()}`")

        return "\n".join(parts)
