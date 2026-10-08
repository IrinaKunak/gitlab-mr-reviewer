"""Review Bridge — the KnowledgeSource over Telegram: asks AIManager questions in
the dedicated group and archives tester reports there.

Independent of the notification channels: its own bot token (BRIDGE_BOT_TOKEN,
default TELEGRAM_BOT_TOKEN) and chat (REVIEW_BRIDGE_CHAT_ID) — turning the
Telegram notifications off does not touch it.

Protocol (see plans/2026-06-10-review-bridge.md):
- one focused plain-text question per message, Jira key included when known;
- AIManager answers via answerGuestQuery (arrives as a regular group message to us)
  plus possible overflow messages — we collect until a quiet grace window passes;
- the trailing usage footer line ("sonnet: ...") is metadata — stripped;
- expect 15-60s latency and possible "not found" answers.

Operational requirements:
- this bot's getUpdates must not be consumed by anything else (exclusive polling);
- the bot must SEE group messages: Bot API group privacy mode disabled via
  @BotFather /setprivacy, or the bot is a group admin — otherwise AIManager's
  answers never reach us.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from ...config import BridgeSection
from ..notify.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

# last line like "sonnet: 12.3k tok · $0.04" — model-ish name, colon, digits somewhere
_FOOTER_RE = re.compile(r"^[\w.\-/ ]{1,40}:\s.*\d.*$")


def strip_usage_footer(text: str) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) >= 2:
        last = lines[-1].strip()
        if len(last) <= 120 and _FOOTER_RE.match(last):
            return "\n".join(lines[:-1]).rstrip()
    return text.rstrip()


class _RateWindow:
    """Sliding one-hour window so we stay under AIManager's GUEST_ANSWER_RATE_PER_HOUR."""

    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self.stamps: list[float] = []

    def allow(self) -> bool:
        now = time.monotonic()
        self.stamps = [stamp for stamp in self.stamps if now - stamp < 3600]
        if len(self.stamps) >= self.per_hour:
            return False
        self.stamps.append(now)
        return True


class ReviewBridge:
    def __init__(self, cfg: BridgeSection, telegram: TelegramClient) -> None:
        self.cfg = cfg
        # questions go out through the same bot that polls for the answers
        self.telegram = telegram
        token = telegram.token
        self._token = token
        self.enabled = bool(cfg.enabled and cfg.chat_id and token)
        self._offset = 0
        self._inbox: asyncio.Queue[dict] = asyncio.Queue()
        self._listener_task: asyncio.Task | None = None
        self._ask_lock = asyncio.Lock()  # one outstanding question at a time
        self._rate = _RateWindow(cfg.rate_per_hour)
        self._bot_id = token.split(":", 1)[0] if token else ""

    # --- lifecycle ---

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Review Bridge disabled (flag/chat_id/token missing)")
            return
        self._listener_task = asyncio.create_task(self._listen(), name="bridge-listener")
        logger.info("Review Bridge listener started (chat %s)", self.cfg.chat_id)

    async def stop(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

    async def _listen(self) -> None:
        """Exclusive getUpdates long-poll; bridge-chat messages go to the inbox."""
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        kwargs: dict[str, Any] = {"timeout": 70.0}
        if self.telegram.proxy_url:
            kwargs["proxy"] = self.telegram.proxy_url
        async with httpx.AsyncClient(**kwargs) as client:
            while True:
                try:
                    response = await client.get(url, params={
                        "offset": self._offset, "timeout": 50})
                    response.raise_for_status()
                    for update in response.json().get("result", []):
                        self._offset = max(self._offset, update["update_id"] + 1)
                        self._dispatch(update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — keep polling through blips
                    logger.warning("bridge getUpdates error: %s",
                                   self.telegram.redact_token(str(exc)))
                    await asyncio.sleep(5)

    def _dispatch(self, update: dict) -> None:
        # Bot API 10.0 may deliver bot-to-bot traffic as guest_* update types —
        # accept both plain messages and any update carrying a message-shaped payload.
        message = update.get("message") or update.get("guest_message") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != str(self.cfg.chat_id):
            return
        sender = message.get("from", {})
        sender_id = str(sender.get("id", ""))
        if sender_id == self._bot_id:
            return  # our own question echoed back
        if self.cfg.answer_bot_id:
            if sender_id != self.cfg.answer_bot_id:
                return  # restricted to AIManager when configured
        elif not sender.get("is_bot"):
            return  # unconfigured: accept bot answers only (humans in the group are observers)
        text = message.get("text") or message.get("caption") or ""
        if text:
            self._inbox.put_nowait({"from_id": sender_id, "text": text,
                                    "date": message.get("date", 0)})

    # --- asking ---

    async def ask(self, question: str) -> str | None:
        """Post one question, collect the (possibly multi-message) answer.

        Returns the cleaned answer text, or None on timeout/disabled/rate-limited.
        Never raises — the investigator must proceed without context if the
        bridge is unavailable.
        """
        if not self.enabled:
            return None
        if not self._rate.allow():
            logger.warning("bridge hourly rate window exhausted; skipping question")
            return None
        # one focused question — also caps the prompt-injection blast radius
        # (repo/MR content cannot be exfiltrated wholesale through the bridge)
        question = question.strip()[:800]

        async with self._ask_lock:
            # drain stale inbox entries from previous interactions
            while not self._inbox.empty():
                self._inbox.get_nowait()

            sent = await self.telegram.send_message(self.cfg.chat_id, question,
                                                    parse_mode=None)
            if not sent:
                return None
            logger.info("bridge question sent: %s", question[:200])

            chunks: list[str] = []
            deadline = time.monotonic() + self.cfg.question_timeout
            while True:
                remaining = deadline - time.monotonic()
                # once an answer started, wait only the short grace window for overflow
                wait = self.cfg.answer_grace if chunks else remaining
                if remaining <= 0 or wait <= 0:
                    break
                try:
                    item = await asyncio.wait_for(
                        self._inbox.get(), timeout=min(wait, remaining))
                    chunks.append(item["text"])
                except TimeoutError:
                    if chunks:
                        break  # grace window passed — answer complete
                    # else keep waiting until the hard deadline

            if not chunks:
                logger.warning("bridge question timed out after %ss",
                               self.cfg.question_timeout)
                return None
            answer = strip_usage_footer("\n".join(chunks))
            logger.info("bridge answer received (%d chars, %d chunks)",
                        len(answer), len(chunks))
            return answer

    # --- archiving ---

    async def archive(self, filename: str, content: bytes, caption: str = "") -> None:
        """Post a document to the bridge chat: AIManager archives tester reports
        into its corpus. Needs only the chat and the token (not BRIDGE=on)."""
        if not (self.cfg.chat_id and self._token):
            return
        try:
            await self.telegram.send_document(self.cfg.chat_id, filename, content, caption)
        except Exception as exc:  # noqa: BLE001 — KnowledgeSource contract: never raise
            logger.warning("bridge archive failed: %s", self.telegram.redact_token(str(exc)))
