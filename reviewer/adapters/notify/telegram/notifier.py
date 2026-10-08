"""The Telegram notification channel: events -> formatted messages -> chats.

Routing (unchanged from v1): review/started messages and alerts go to every
TELEGRAM_CHAT_IDS chat; tester reports to TESTER_REPORT_CHAT_IDS (default:
the same chats). Nothing is sent unless TELEGRAM=on with a token and chats.
"""

from __future__ import annotations

import logging

from ....config import TelegramSection
from ....domain.events import (
    NotificationEvent,
    ReviewFailed,
    ReviewPosted,
    ReviewStarted,
    SystemAlert,
    TesterReportReady,
)
from .client import MESSAGE_LIMIT, TelegramClient
from .formatter import TelegramFormatter

logger = logging.getLogger(__name__)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Telegram rejects texts over 4096 chars: cut on line breaks where possible."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class TelegramNotifier:
    name = "telegram"

    def __init__(self, cfg: TelegramSection, client: TelegramClient,
                 formatter: TelegramFormatter, *,
                 exclude_document_chats: tuple[str, ...] = ()) -> None:
        self.cfg = cfg
        self.client = client
        self.formatter = formatter
        # chats that already get the tester report another way (the bridge
        # chat, where AIManager archives it) — never send it twice
        self.exclude_document_chats = exclude_document_chats

    @property
    def active(self) -> bool:
        return bool(self.cfg.enabled and self.client.token and self.cfg.chat_ids)

    def tester_report_chats(self) -> list[str]:
        chats: list[str] = []
        for chat_id in self.cfg.tester_report_chat_ids or self.cfg.chat_ids:
            if chat_id not in chats and chat_id not in self.exclude_document_chats:
                chats.append(chat_id)
        return chats

    async def notify(self, event: NotificationEvent) -> None:
        try:
            await self._dispatch(event)
        except Exception:  # noqa: BLE001 — the Notifier contract: never raise
            logger.exception("telegram notification failed for %s", type(event).__name__)

    async def _dispatch(self, event: NotificationEvent) -> None:
        if not self.active:
            logger.debug("Telegram notifications disabled or not configured")
            return
        fmt = self.formatter
        if isinstance(event, ReviewStarted):
            await self._broadcast(fmt.mr_message(event.mr, event.has_conflicts,
                                                 language=event.language))
        elif isinstance(event, ReviewPosted):
            await self._broadcast(fmt.mr_message(event.mr, event.has_conflicts,
                                                 event.review_text, event.usage,
                                                 language=event.language))
        elif isinstance(event, ReviewFailed | SystemAlert):
            await self._broadcast(fmt.error_message(event))
        elif isinstance(event, TesterReportReady):
            caption = fmt.tester_report_caption(event)
            for chat_id in self.tester_report_chats():
                await self.client.send_document(chat_id, event.filename, event.content,
                                                caption)

    async def _broadcast(self, text: str) -> None:
        for chat_id in self.cfg.chat_ids:
            for chunk in split_message(text):
                await self.client.send_message(chat_id, chunk)
