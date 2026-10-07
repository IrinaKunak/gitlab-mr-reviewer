"""Telegram sink: replaces the two wire-level senders, so message formatting
(notify, notify_error, format_mr_message) runs for real."""

from __future__ import annotations

from dataclasses import dataclass, field


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


@dataclass
class FakeTelegram:
    messages: list[SentMessage] = field(default_factory=list)
    documents: list[SentDocument] = field(default_factory=list)

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
