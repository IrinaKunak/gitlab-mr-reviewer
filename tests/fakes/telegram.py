"""Telegram sink: a TelegramClient transport whose two wire-level senders
record instead of sending, so the real TelegramNotifier (formatting, routing)
runs on top of it."""

from __future__ import annotations

from dataclasses import dataclass

from reviewer.adapters.notify.telegram import TelegramClient


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
    def __init__(self, token: str = "test-token") -> None:
        super().__init__(token)
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
