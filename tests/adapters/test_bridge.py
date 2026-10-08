"""Review Bridge answer routing (stage 21.3, #16): answers find their question
by reply_to_message, so questions no longer need a global lock."""

from __future__ import annotations

import asyncio
import itertools

from reviewer.adapters.knowledge import ReviewBridge
from reviewer.adapters.knowledge import telegram_bridge as bridge_mod
from reviewer.config import BridgeSection
from tests.fakes import FakeTelegram

AIMANAGER = {"id": 42, "is_bot": True}


class Transport(FakeTelegram):
    def __init__(self):
        super().__init__("777:token")
        self.ids = itertools.count(1000)
        self.sent: list[tuple[int, str]] = []

    async def post_message(self, chat_id, text, *, parse_mode="Markdown"):
        message_id = next(self.ids)
        self.sent.append((message_id, text))
        return message_id


def _bridge(**cfg):
    values = {"enabled": True, "chat_id": "-100bridge", "question_timeout": 2,
              "answer_grace": 0.05, **cfg}
    transport = Transport()
    return ReviewBridge(BridgeSection(**values), transport), transport


def _answer(bridge, text, reply_to=None):
    message = {"chat": {"id": "-100bridge"}, "from": AIMANAGER, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    bridge._dispatch({"update_id": 1, "message": message})


async def _wait_sent(transport, n):
    while len(transport.sent) < n:
        await asyncio.sleep(0.001)


def test_parallel_questions_get_their_own_linked_answers():
    bridge, transport = _bridge(max_parallel=2)

    async def scenario():
        first = asyncio.create_task(bridge.ask("What is PAY-1 about?"))
        second = asyncio.create_task(bridge.ask("What is PAY-2 about?"))
        await _wait_sent(transport, 2)
        (id1, _), (id2, _) = transport.sent
        _answer(bridge, "PAY-2: refunds", reply_to=id2)  # answered out of order
        _answer(bridge, "PAY-1: invoices", reply_to=id1)
        _answer(bridge, "more on invoices\nsonnet: 1.2k tok · $0.01", reply_to=id1)
        return await first, await second

    assert asyncio.run(scenario()) == ("PAY-1: invoices\nmore on invoices", "PAY-2: refunds")
    assert bridge._open == {}


def test_unlinked_answer_goes_to_the_single_open_question():
    bridge, transport = _bridge()

    async def scenario():
        task = asyncio.create_task(bridge.ask("q"))
        await _wait_sent(transport, 1)
        _answer(bridge, "answer without reply_to")
        return await task

    assert asyncio.run(scenario()) == "answer without reply_to"


def test_late_answer_is_dropped_not_given_to_the_next_question():
    bridge, transport = _bridge()
    bridge.cfg.question_timeout = 0.05  # type: ignore[assignment]  # (an int setting)

    async def scenario():
        assert await bridge.ask("slow question") is None  # timed out
        (late_id, _), = transport.sent
        task = asyncio.create_task(bridge.ask("next question"))
        await _wait_sent(transport, 2)
        _answer(bridge, "late answer to the first", reply_to=late_id)  # no longer open
        return await task

    # the late, linked answer does not leak into the next question
    assert asyncio.run(scenario()) is None


def test_default_keeps_questions_serialized():
    bridge, transport = _bridge()  # BRIDGE_MAX_PARALLEL=1

    async def scenario():
        first = asyncio.create_task(bridge.ask("one"))
        second = asyncio.create_task(bridge.ask("two"))
        await _wait_sent(transport, 1)
        await asyncio.sleep(0.02)
        assert len(transport.sent) == 1  # the second waits for a slot
        _answer(bridge, "first answer", reply_to=transport.sent[0][0])
        await first
        await _wait_sent(transport, 2)
        _answer(bridge, "second answer", reply_to=transport.sent[1][0])
        return await second

    assert asyncio.run(scenario()) == "second answer"


def test_strip_usage_footer():
    text = "Issue PBV-123 is about cart totals.\nAcceptance: totals match.\nsonnet: 12.3k tok $0.04"
    assert bridge_mod.strip_usage_footer(text).endswith("totals match.")
    # multi-line answers without a footer are untouched
    clean = "line one\nline two"
    assert bridge_mod.strip_usage_footer(clean) == clean
    # a colon line WITHOUT digits is content, not a footer
    keep = "Steps:\nDo the thing:"
    assert bridge_mod.strip_usage_footer(keep) == keep


def test_rate_window():
    window = bridge_mod._RateWindow(2)
    assert window.allow() and window.allow()
    assert not window.allow()
