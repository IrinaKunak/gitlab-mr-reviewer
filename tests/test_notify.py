"""Notifier port (stage 16): the contract every channel must meet, the
composite/null channels, the channel registry, and the bridge's independence
from the notification channels.

A new channel (Bitrix24) joins CHANNELS below and must pass the contract."""

from __future__ import annotations

import asyncio

import pytest

from reviewer.adapters.notify import CompositeNotifier, NullNotifier
from reviewer.adapters.notify.telegram import (
    TelegramFormatter,
    TelegramNotifier,
    split_message,
)
from reviewer.bootstrap import build_notifier, build_services
from reviewer.config import TelegramSection
from reviewer.domain.events import (
    MrSummary,
    ReviewFailed,
    ReviewPosted,
    ReviewStarted,
    SystemAlert,
    TesterReportReady,
    UsageSummary,
)
from tests.factories import make_services, make_settings, review_job
from tests.fakes import FakeTelegram

MR = MrSummary.from_job(review_job(mr_iid=7, title="Add refunds"))
EVENTS = [
    ReviewStarted(MR),
    ReviewPosted(MR, "Verdict: ship it.", usage=UsageSummary()),
    ReviewFailed("ai_failure", "boom", mr=MR, job_id="j1"),
    TesterReportReady(MR, "report.md", b"# steps"),
    SystemAlert("webhook_error", "bad token"),
]


class BrokenTransport(FakeTelegram):
    async def send_message(self, chat_id, text, *, parse_mode="Markdown"):
        raise RuntimeError("network down")

    async def send_document(self, chat_id, filename, content, caption=""):
        raise RuntimeError("network down")


def _telegram(transport):
    return TelegramNotifier(TelegramSection(enabled=True, token="t", chat_ids=["c1"]),
                            transport, TelegramFormatter("en"))


CHANNELS = {
    "telegram": lambda: _telegram(FakeTelegram()),
    "telegram-broken-transport": lambda: _telegram(BrokenTransport()),
    "null": NullNotifier,
    "composite": lambda: CompositeNotifier([_telegram(BrokenTransport()), NullNotifier()]),
}


@pytest.mark.parametrize("make", CHANNELS.values(), ids=CHANNELS.keys())
@pytest.mark.parametrize("event", EVENTS, ids=lambda e: type(e).__name__)
def test_notifier_contract_never_raises(make, event):
    notifier = make()
    assert isinstance(notifier.name, str) and notifier.name
    asyncio.run(notifier.notify(event))  # whatever happens inside: no exception


def test_composite_isolates_a_failing_channel():
    class Exploding:
        name = "exploding"

        async def notify(self, event):
            raise RuntimeError("contract breach")

    tail = NullNotifier()
    asyncio.run(CompositeNotifier([Exploding(), tail]).notify(EVENTS[0]))
    assert tail.events == [EVENTS[0]]


def test_telegram_channel_routing_and_splitting():
    tg = FakeTelegram()
    cfg = TelegramSection(enabled=True, token="t", chat_ids=["c1", "c2"],
                          tester_report_chat_ids=["testers"])
    notifier = TelegramNotifier(cfg, tg, TelegramFormatter("en"),
                                exclude_document_chats=("bridge",))
    for event in EVENTS:
        asyncio.run(notifier.notify(event))
    # every message to every chat; the report only to the testers' chat
    assert [m.chat_id for m in tg.messages] == ["c1", "c2"] * 4
    assert [(d.chat_id, d.filename) for d in tg.documents] == [("testers", "report.md")]

    # disabled channel: silent
    tg2 = FakeTelegram()
    off = TelegramNotifier(TelegramSection(enabled=False, token="t", chat_ids=["c1"]), tg2,
                           TelegramFormatter("en"))
    asyncio.run(off.notify(EVENTS[2]))
    assert tg2.messages == []

    long = "line\n" * 2000  # 10k chars
    chunks = split_message(long)
    assert all(len(c) <= 4096 for c in chunks) and "".join(chunks).count("line") == 2000


def test_full_review_with_null_notifier(world):
    from tests.fakes import file_change, mr_webhook

    project = world.gitlab.add_project(1, "group/app", files={"a.py": "x = 1\n"})
    mr = project.add_mr(7, changes=[file_change("a.py", "-x = 0\n+x = 1\n")])
    world.llm.on_complete(
        {"complexity": "trivial", "risk_areas": [], "jira_keys": [],
         "needs_investigation": False, "summary": "s", "skip_globs": []},
        "Verdict: fine.", "Вердикт: всё в порядке.")
    null = NullNotifier()
    world.services.review_mr.notifier = null
    world.services.review_mr.deliver.notifier = null

    world.send(mr_webhook(project, mr))

    assert "Вердикт: всё в порядке." in mr.bot_notes[-1]
    assert [type(e).__name__ for e in null.events] == ["ReviewStarted", "ReviewPosted"]
    assert world.telegram.messages == []


def test_channel_registry(tmp_path):
    cfg = make_settings(tmp_path)
    assert cfg.notify.channels == ["telegram"]
    assert [c.name for c in build_notifier(cfg).channels] == ["telegram"]

    cfg.notify.channels = []
    assert build_notifier(cfg).channels == []  # nothing configured: nothing sent

    # bitrix validates (it is a known channel) but is not built yet
    cfg = make_settings(tmp_path, notify__channels=["telegram", "bitrix"])
    with pytest.raises(SystemExit, match="bitrix.*not implemented"):
        build_services(cfg, ai=object(), bridge=object(), repo_cache=object(),
                       vcs_for=lambda i: None, workers=0)


def test_channels_env_validation(monkeypatch):
    from reviewer.config import load_settings

    monkeypatch.setenv("NOTIFY_CHANNELS", "telegram,bitrix")
    assert load_settings().notify.channels == ["telegram", "bitrix"]
    monkeypatch.setenv("NOTIFY_CHANNELS", "slack")
    with pytest.raises(SystemExit, match="NOTIFY_CHANNELS"):
        load_settings()


def test_bridge_has_its_own_bot_and_survives_telegram_off(tmp_path):
    cfg = make_settings(tmp_path, bridge__enabled=True, bridge__chat_id="-100bridge")
    cfg.notify.telegram.enabled = False
    cfg.notify.telegram.token = "notify-bot"
    svc = make_services(cfg, bridge=None)
    assert svc.bridge.enabled and svc.bridge.telegram.token == "notify-bot"  # default

    cfg.bridge.bot_token = "bridge-bot"
    svc = make_services(cfg, bridge=None)
    assert svc.bridge.enabled and svc.bridge.telegram.token == "bridge-bot"
