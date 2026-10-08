"""Telegram Bot API transport (async httpx): sendMessage / sendDocument.

Wire level only — formatting lives in formatter.py, routing in notifier.py.
Used by the Telegram notification channel and, with its own token/chat, by
the Review Bridge (adapters/knowledge/telegram_bridge.py).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
MESSAGE_LIMIT = 4096  # Bot API sendMessage text limit (characters)
CAPTION_LIMIT = 1024


class TelegramClient:
    """`send_message` / `send_document` are the wire level — a test fake
    overrides just those, so formatting and routing run for real."""

    def __init__(self, token: str, *, proxy_url: str | None = None) -> None:
        self.token = token
        self.proxy_url = proxy_url

    def redact_token(self, text: str) -> str:
        """httpx exceptions embed the request URL, which contains the bot token."""
        if self.token:
            return text.replace(self.token, "***TOKEN***")
        return text

    def _client(self, timeout: float = 15.0) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return httpx.AsyncClient(**kwargs)

    async def send_message(self, chat_id: str, text: str, *,
                           parse_mode: str | None = "Markdown") -> bool:
        if not self.token:
            return False
        data: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                "disable_web_page_preview": True}
        if parse_mode:
            data["parse_mode"] = parse_mode
        try:
            async with self._client() as client:
                response = await client.post(f"{API}/bot{self.token}/sendMessage", json=data)
                if response.status_code != 200 and parse_mode:
                    # Markdown parse failures are common with code in MR titles — retry plain
                    data.pop("parse_mode", None)
                    response = await client.post(
                        f"{API}/bot{self.token}/sendMessage", json=data)
                response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 — notifications must never break a review
            logger.error("Telegram sendMessage to %s failed: %s", chat_id,
                         self.redact_token(str(exc)))
            return False

    async def send_document(self, chat_id: str, filename: str, content: bytes,
                            caption: str = "") -> bool:
        if not self.token:
            return False
        try:
            async with self._client(timeout=60.0) as client:
                response = await client.post(
                    f"{API}/bot{self.token}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption[:CAPTION_LIMIT]},
                    files={"document": (filename, content, "text/markdown")},
                )
                response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Telegram sendDocument to %s failed: %s", chat_id,
                         self.redact_token(str(exc)))
            return False
