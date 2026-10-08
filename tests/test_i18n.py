"""i18n catalog (stage 15): ru and en have the same keys, and every message the
service sends is identical, character for character, to the pre-catalog text
(tests/snapshots/messages.json was captured from the inline tables)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from reviewer.application import content
from reviewer.i18n import catalog, languages, t
from tests.factories import review_job
from tests.fakes import FakeTelegram

SNAPSHOT = json.loads((Path(__file__).parent / "snapshots" / "messages.json")
                      .read_text(encoding="utf-8"))
ERROR_KINDS = ("ai_failure", "gemini_failure", "gitlab_api_error", "webhook_error",
               "timeout", "general", "prompt_cache", "weird")


def test_catalogs_have_the_same_keys():
    assert languages() == ["en", "ru"]
    assert set(catalog("en")) == set(catalog("ru"))


def test_missing_key_falls_back_to_english_with_warning(monkeypatch, caplog):
    monkeypatch.setitem(catalog("ru"), "mr.no_changes", None)
    monkeypatch.delitem(catalog("ru"), "mr.no_changes")
    with caplog.at_level(logging.WARNING, logger="reviewer.i18n"):
        assert t("mr.no_changes", "ru") == t("mr.no_changes", "en")
    assert "missing in ru" in caplog.text
    assert t("mr.no_changes", "de") == t("mr.no_changes", "en")  # unknown language
    with pytest.raises(KeyError):
        t("mr.nonexistent", "en")


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_mr_notes_match_previous_texts(lang):
    for key, table in SNAPSHOT["mr_notes"].items():
        assert t(key, lang, job_id="{job_id}", link="{link}") == table[lang], key
    assert content.format_review_comment(" body \n", lang) == SNAPSHOT[f"comment_{lang}"]


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_telegram_texts_match_previous_texts(lang):
    tg = FakeTelegram(language=lang)
    job = review_job(title="Fix `x`", author="dev", source_branch="f", target_branch="main",
                     mr_iid=5, url="https://g/p/-/merge_requests/5")
    assert (tg.format_mr_message(job, "g/p", False, gitlab_instance="https://gitlab.test/")
            == SNAPSHOT[f"mr_new_{lang}"])
    assert (tg.format_mr_message(job, "g/p", True, "short review",
                                 gitlab_instance="https://gitlab.test")
            == SNAPSHOT[f"mr_conf_{lang}"])
    assert tg.format_mr_message(job, "g/p", False, "x" * 2500) == SNAPSHOT[f"mr_long_{lang}"]
    for kind in ERROR_KINDS:
        asyncio.run(tg.notify_error(kind, "details", {
            "project_id": 1, "mr_iid": 2, "gitlab_instance": "primary", "job_id": "j1"}))
        assert tg.messages[-1].text.rsplit("\n", 1)[0] == SNAPSHOT[f"err_{kind}_{lang}"], kind
