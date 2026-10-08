"""Telegram sink: a TelegramClient whose two wire-level senders record instead
of sending, so message formatting (notify, notify_error, format_mr_message)
runs for real."""

from __future__ import annotations

from dataclasses import dataclass

from reviewer.config import TelegramSection
from reviewer.telegram_io import TelegramClient


@dataclass
class SentMessage:
    chat_id: str
    text: str


@dataclass
class SentDocument:
    chat_id: str
    filename: str
    content: bytes
    caption: str


class FakeTelegram(TelegramClient):
    def __init__(self, cfg: TelegramSection | None = None, *, language: str = "en") -> None:
        super().__init__(cfg or TelegramSection(enabled=True, token="test-token",
                                                chat_ids=["chat-1"]), language=language)
        self.messages: list[SentMessage] = []
        self.documents: list[SentDocument] = []

    async def send_message(self, chat_id: str, text: str, *,
                           parse_mode: str | None = "Markdown") -> bool:
        self.messages.append(SentMessage(chat_id, text))
        return True

    async def send_document(self, chat_id: str, filename: str, content: bytes,
                            caption: str = "") -> bool:
        self.documents.append(SentDocument(chat_id, filename, content, caption))
        return True

    @property
    def errors(self) -> list[str]:
        return [m.text for m in self.messages if "🚨" in m.text]

    @property
    def notifications(self) -> list[str]:
        return [m.text for m in self.messages if "🚨" not in m.text]
