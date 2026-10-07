"""Characterization scenarios: webhook in -> GitLab notes / Telegram / usage out.

These pin the service's OBSERVABLE behavior before the refactoring (stages
6-21) moves code around: only public entry points (HTTP webhook) are driven,
and only external effects are asserted — note texts, messages, AI tiers,
usage entries. User-facing strings are literal on purpose: a refactoring
that changes them must change these tests deliberately.
"""

from __future__ import annotations

import re

from reviewer.ai_client import AIError
from reviewer.config import settings
from tests.fakes import AgentScript, ToolCall, file_change, mr_webhook

RU_START = "🤖 Начинаем автоматический обзор кода..."
RU_REVIEW_HEADER = "## 🤖 Автоматический обзор кода"
RU_CONFLICT = ("⚠️ Запрос на слияние имеет конфликты. Обзор кода пропущен до "
               "разрешения конфликтов.")


def triage(complexity: str, **extra) -> dict:
    return {"complexity": complexity, "risk_areas": [], "jira_keys": [],
            "needs_investigation": False, "summary": "change", "skip_globs": [],
            **extra}


def _project_with_mr(world, *, changes, files=None, **mr_kwargs):
    project = world.gitlab.add_project(1, "group/app", files=files or {})
    mr = project.add_mr(7, changes=changes, **mr_kwargs)
    return project, mr


# --- 4.3 trivial MR: Haiku all the way ---

def test_trivial_mr_reviewed_by_fast_tier(world):
    project, mr = _project_with_mr(
        world, files={"README.md": "# App\nInstal it\n"},
        changes=[file_change("README.md", "-Instal it\n+Install it\n")],
        title="Fix typo in README")
    world.llm.on_complete(
        triage("trivial"),
        "Verdict: looks good. Typo fix only.",
        "Вердикт: всё в порядке. Только исправление опечатки.")

    resp = world.send(mr_webhook(project, mr))

    assert resp == {"status_code": 200, "status": "accepted", "merge_request": 7,
                    "instance": "primary"}
    # triage -> trivial review -> translation, all on the fast tier
    assert [(c.method, c.tier) for c in world.llm.calls] == [
        ("complete_json", "fast"), ("complete", "fast"), ("complete", "fast")]
    assert {c.model for c in world.llm.calls} == {settings.model_fast}
    assert world.repo.checkouts == []  # trivial MRs never clone

    start, review = mr.bot_notes
    assert start == RU_START
    assert review.startswith(RU_REVIEW_HEADER)
    assert "Вердикт: всё в порядке. Только исправление опечатки." in review
    assert "Verdict" not in review  # the English draft is never published

    started, reviewed = world.telegram.notifications
    assert "group/app" in started and "Fix typo in README" in started
    assert "Вердикт: всё в порядке" in reviewed
    assert world.telegram.errors == []

    (entry,) = world.usage_entries()
    assert entry["kind"] == "review" and entry["mr_iid"] == 7
    assert entry["instance"] == "primary" and entry["project"] == "group/app"
    assert set(entry["models"]) == {settings.model_fast}


# --- 4.4 normal MR: main tier verifies with repo tools ---

def test_normal_mr_reviewed_with_repo_tools(world):
    files = {"billing/charge.py": "def charge(amount):\n    return amount * 100\n",
             "billing/api.py": "from billing.charge import charge\n\n"
                               "def pay(order):\n    return charge(order.total)\n"}
    project, mr = _project_with_mr(
        world, files=files,
        changes=[file_change("billing/charge.py",
                             "-    return amount\n+    return amount * 100\n")],
        title="Charge in cents")
    world.llm.on_complete(
        triage("normal", risk_areas=["payments"]),
        "Вердикт: одна проблема. billing/api.py:4 передаёт рубли в charge().")
    world.llm.on_agent(AgentScript(
        tool_calls=[ToolCall("repo_grep", {"pattern": r"charge\("})],
        final_text="Verdict: one issue. billing/api.py:4 passes roubles to charge()."))

    world.send(mr_webhook(project, mr))

    assert [(c.method, c.tier) for c in world.llm.calls] == [
        ("complete_json", "fast"), ("agent_loop", "main"), ("complete", "fast")]
    review_call = world.llm.calls[1]
    assert review_call.model == settings.model_main
    assert "payments" in review_call.system  # triage risk areas reach the reviewer
    # the tool ran against the checkout at the MR head and found the call site
    (tool, output), = review_call.tool_results
    assert tool == "repo_grep" and "billing/api.py:4" in output
    assert world.repo.checkouts == [("group/app", 7, "sha-1")]
    assert len(world.repo.released) == 1

    start, review = mr.bot_notes
    assert start == RU_START
    assert "billing/api.py:4 передаёт рубли в charge()" in review
    assert "Вердикт: одна проблема" in world.telegram.notifications[-1]

    (entry,) = world.usage_entries()
    assert set(entry["models"]) == {settings.model_fast, settings.model_main}


# --- 4.5 AI failure: neutral MR note (stage 2), details only in the alert ---

def test_ai_failure_posts_neutral_note_and_alerts(world):
    leak = "529 overloaded at https://gw.internal.example/anthropic/v1/messages"
    project, mr = _project_with_mr(
        world, files={"app.py": "x = 1\n"},
        changes=[file_change("app.py", "-x = 0\n+x = 1\n")])
    world.llm.on_complete(triage("normal"), AIError(leak))
    world.llm.on_agent(AIError(leak))  # tool review fails -> plain review fails too

    world.send(mr_webhook(project, mr))

    assert [(c.method, c.tier) for c in world.llm.calls] == [
        ("complete_json", "fast"), ("agent_loop", "main"), ("complete", "main")]
    start, failure = mr.bot_notes
    assert start == RU_START
    match = re.fullmatch(r"❌ Ревью не выполнено, id задачи: ([0-9a-f]{8})", failure)
    assert match, failure
    job_id = match.group(1)

    (alert,) = world.telegram.errors
    assert "Ошибка AI" in alert and "gw.internal.example" in alert
    assert job_id in alert
    assert "gw.internal.example" not in "".join(mr.bot_notes)
    assert len(world.repo.released) == len(world.repo.checkouts) == 1


# --- 4.6 nothing to review: conflicts, merged/closed MRs ---

def test_conflicting_mr_is_not_reviewed(world):
    project, mr = _project_with_mr(
        world, changes=[file_change("app.py", "+x = 1\n")],
        merge_status="cannot_be_merged", has_conflicts=True)

    world.send(mr_webhook(project, mr))

    assert mr.bot_notes == [RU_CONFLICT]
    assert world.llm.calls == []
    (started,) = world.telegram.notifications  # the MR is still announced
    assert "КОНФЛИКТ" in started
    assert world.usage_entries() == []


def test_mr_merged_while_queued_is_not_reviewed(world):
    project, mr = _project_with_mr(world, changes=[file_change("app.py", "+x = 1\n")])
    payload = mr_webhook(project, mr, "update")
    mr.state = "merged"  # merged between the webhook and the worker picking it up

    assert world.send(payload)["status"] == "accepted"

    assert mr.bot_notes == []
    assert world.llm.calls == []
    assert world.telegram.messages == []


def test_close_event_is_ignored_at_the_door(world):
    project, mr = _project_with_mr(world, changes=[file_change("app.py", "+x = 1\n")])

    resp = world.send(mr_webhook(project, mr, "close"))

    assert resp["status"] == "ignored"
    assert world.gitlab.calls == []  # not even an API call
    assert world.telegram.messages == []
