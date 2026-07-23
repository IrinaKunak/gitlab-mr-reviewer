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
from .config import settings

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"


def redact_token(text: str) -> str:
    """httpx exceptions embed the request URL, which contains the bot token."""
    if settings.telegram_token:
        return text.replace(settings.telegram_token, "***TOKEN***")
    return text


def _client(timeout: float = 15.0) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": timeout}
    if settings.proxy_url:
        kwargs["proxy"] = settings.proxy_url
    return httpx.AsyncClient(**kwargs)


async def send_message(chat_id: str, text: str, *, parse_mode: str | None = "Markdown") -> bool:
    if not settings.telegram_token:
        return False
    data: dict[str, Any] = {"chat_id": chat_id, "text": text,
                            "disable_web_page_preview": True}
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        async with _client() as client:
            response = await client.post(
                f"{_API}/bot{settings.telegram_token}/sendMessage", json=data)
            if response.status_code != 200 and parse_mode:
                # Markdown parse failures are common with code in MR titles — retry plain
                data.pop("parse_mode", None)
                response = await client.post(
                    f"{_API}/bot{settings.telegram_token}/sendMessage", json=data)
            response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — notifications must never break the pipeline
        logger.error("Telegram sendMessage to %s failed: %s", chat_id, redact_token(str(exc)))
        return False


async def send_document(chat_id: str, filename: str, content: bytes, caption: str = "") -> bool:
    if not settings.telegram_token:
        return False
    try:
        async with _client(timeout=60.0) as client:
            response = await client.post(
                f"{_API}/bot{settings.telegram_token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (filename, content, "text/markdown")},
            )
            response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Telegram sendDocument to %s failed: %s", chat_id, redact_token(str(exc)))
        return False


async def notify(message: str, is_error: bool = False) -> bool:
    """Broadcast to all configured notification channels (v1 behavior)."""
    if not settings.telegram_enabled or not settings.telegram_token or not settings.telegram_chat_ids:
        logger.debug("Telegram notifications disabled or not configured")
        return False
    if is_error:
        prefix = "🚨 **ERROR** 🚨\n" if settings.review_language == "en" else "🚨 **ОШИБКА** 🚨\n"
        message = prefix + message
    sent = 0
    for chat_id in settings.telegram_chat_ids:
        if await send_message(chat_id, message):
            sent += 1
    return sent > 0


_ERROR_TITLES = {
    "ru": {
        "ai_failure": "Ошибка AI", "gemini_failure": "Ошибка AI",
        "gitlab_api_error": "Ошибка GitLab API",
        "webhook_error": "Ошибка обработки webhook",
        "timeout": "Превышено время ожидания", "general": "Общая ошибка",
    },
    "en": {
        "ai_failure": "AI Error", "gemini_failure": "AI Error",
        "gitlab_api_error": "GitLab API Error",
        "webhook_error": "Webhook Processing Error",
        "timeout": "Timeout Error", "general": "General Error",
    },
}


async def notify_error(error_type: str, error_details: str,
                       context: dict[str, Any] | None = None) -> bool:
    if not settings.telegram_enabled:
        return False
    titles = _ERROR_TITLES.get(settings.review_language, _ERROR_TITLES["en"])
    parts = [f"**{titles.get(error_type, titles['general'])}**",
             f"**Details:** {error_details}"]
    context = context or {}
    if "project_id" in context:
        parts.append(f"**Project ID:** {context['project_id']}")
    if "mr_iid" in context:
        parts.append(f"**MR:** !{context['mr_iid']}")
    if "gitlab_instance" in context:
        parts.append(f"**Instance:** {context['gitlab_instance']}")
    parts.append(f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return await notify("\n".join(parts), is_error=True)


def format_mr_message(mr_data: dict, project_name: str, has_conflicts: bool = False,
                      review_content: str | None = None,
                      gitlab_instance: str | None = None) -> str:
    """v1 notification format, preserved verbatim."""
    lang = settings.review_language
    if has_conflicts:
        status_emoji = "⚠️"
        status_text = "MR with CONFLICTS" if lang == "en" else "MR С КОНФЛИКТАМИ"
    else:
        status_emoji = "✅"
        status_text = "New MR" if lang == "en" else "Новый MR"

    instance_info = ""
    if gitlab_instance:
        domain = gitlab_instance.split("://")[-1].rstrip("/")
        instance_info = f" from `{domain}`"

    parts = [
        f"{status_emoji} **{status_text}{instance_info}**",
        f"**Project:** `{project_name}`",
        f"**Author:** {mr_data['author']}",
        f"**Title:** {mr_data['title']}",
        f"**Branch:** `{mr_data['source_branch']}` → `{mr_data['target_branch']}`",
        f"**Link:** [!{mr_data['mr_iid']}]({mr_data['url']})",
    ]

    if has_conflicts:
        parts.append("")
        parts.append(
            "🚫 **BLOCKED: Merge conflicts must be resolved before merging!**"
            if lang == "en" else
            "🚫 **ЗАБЛОКИРОВАН: Конфликты слияния должны быть разрешены перед слиянием!**")

    if review_content and len(review_content) < 2000:
        parts.append("\n📝 **Code Review:**" if lang == "en" else "\n📝 **Обзор кода:**")
        truncated = review_content[:1500] + "..." if len(review_content) > 1500 else review_content
        parts.append(f"```\n{truncated}\n```")
    elif review_content:
        parts.append(
            "\n📝 Code review posted to GitLab (too long for Telegram)"
            if lang == "en" else
            "\n📝 Обзор кода опубликован в GitLab (слишком длинный для Telegram)")

    # usage footer (AIManager style) — only on review-completion messages
    if review_content:
        tracker = usage.current_tracker.get()
        if tracker is not None and tracker.calls:
            parts.append(f"\n`{tracker.footer_line()}`")

    return "\n".join(parts)
