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
from tests.fakes import AgentScript, ToolCall, file_change, mr_webhook, note_webhook

RU_START = "🤖 Начинаем автоматический обзор кода..."
RU_REVIEW_HEADER = "## 🤖 Автоматический обзор кода"
RU_TESTER_REPORT = "## 🧪 Отчёт для тестировщика"
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
    assert {c.model for c in world.llm.calls} == {world.settings.llm.tiers.fast.model}
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
    assert set(entry["models"]) == {world.settings.llm.tiers.fast.model}


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
    assert review_call.model == world.settings.llm.tiers.main.model
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
    tiers = world.settings.llm.tiers
    assert set(entry["models"]) == {tiers.fast.model, tiers.main.model}


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


# =====================================================================
# Stage 5: complex scenarios
# =====================================================================

BILLING_FILES = {
    "billing/refund.py": "def refund(order):\n    return order.total\n",
    "billing/api.py": "from billing.refund import refund\n\n"
                      "def cancel(order):\n    return refund(order)\n",
}


def reviewed_files(user_prompt: str) -> list[str]:
    """Files whose contents the reviewer got (full-context format)."""
    return re.findall(r"^FILE #\d+: (\S+)$", user_prompt, re.MULTILINE)


def _review_round(world, verdict_en: str, verdict_ru: str, **triage_extra) -> None:
    """Script one normal-MR review: triage -> tool review -> translation."""
    world.llm.on_complete(triage("normal", **triage_extra), verdict_ru)
    world.llm.on_agent(AgentScript(final_text=verdict_en))


# --- 5.2 complex MR: investigator + bridge + impact + tester report ---

def test_complex_mr_runs_investigator_with_bridge_and_tester_report(world):
    project, mr = _project_with_mr(
        world, files=BILLING_FILES,
        changes=[file_change("billing/refund.py",
                             "-    return order.total\n+    return order.total - order.fee\n")],
        title="Refund minus fee", source_branch="PAY-123-refund-fee")
    world.llm.on_complete(
        triage("complex", risk_areas=["payments"], jira_keys=["PAY-123"],
               needs_investigation=True),
        "Вердикт: замечаний нет.\n\n---\n\nВлияние: cancel() в billing/api.py:4 "
        "теперь возвращает сумму за вычетом комиссии.",
        "## ОТЧЁТ ДЛЯ ТЕСТИРОВЩИКА\n\n1. Отмените заказ с комиссией.")
    world.llm.on_agent(
        AgentScript(final_text="Verdict: no issues."),
        AgentScript(
            tool_calls=[ToolCall("repo_grep", {"pattern": r"refund\("}),
                        ToolCall("ask_aimanager",
                                 {"question": "PAY-123: should refunds keep the fee?"})],
            final_text="## Impact\ncancel() in billing/api.py:4 now refunds net of fee.\n\n"
                       "## TESTER REPORT\n1. Cancel an order that had a fee."))
    world.bridge.on_ask("PAY-123: yes, the fee is non-refundable since July.\n"
                        "sonnet: 1.2k tok · $0.01")

    world.send(mr_webhook(project, mr))

    assert [(c.method, c.tier) for c in world.llm.calls] == [
        ("complete_json", "fast"), ("agent_loop", "main"), ("agent_loop", "smart"),
        ("complete", "fast"), ("complete", "main")]
    investigation = world.llm.calls[2]
    assert investigation.model == world.settings.llm.tiers.smart.model
    assert "Verdict: no issues." in investigation.user  # it builds on the review
    assert world.bridge.questions == ["PAY-123: should refunds keep the fee?"]
    (_, grep_out), (_, bridge_out) = investigation.tool_results
    assert "billing/api.py:4" in grep_out
    assert "fee is non-refundable" in bridge_out
    # one checkout shared by the tool review and the investigator
    assert world.repo.checkouts == [("group/app", 7, "sha-1")]
    assert len(world.repo.released) == 1

    # impact analysis rides in the review comment, the tester report does not
    review_translation, report_translation = world.llm.calls[3:]
    assert "now refunds net of fee" in review_translation.user
    assert "TESTER REPORT" not in review_translation.user
    assert "Cancel an order that had a fee" in report_translation.user

    start, review, report_note = mr.bot_notes
    assert start == RU_START
    assert review.startswith(RU_REVIEW_HEADER)
    assert "Влияние: cancel() в billing/api.py:4" in review
    filename = "tester-report-group-app-MR7.md"
    assert report_note == (f"{RU_TESTER_REPORT}\n\nИнструкция по проверке этого MR во "
                           f"вложении: [{filename}](/uploads/1/{filename})")
    assert project.uploads == [
        (filename, "## ОТЧЁТ ДЛЯ ТЕСТИРОВЩИКА\n\n1. Отмените заказ с комиссией.".encode())]
    # AIManager archives reports (KnowledgeSource), the team channels get a copy
    assert [(name, caption.split("\n")[0]) for name, _, caption in world.bridge.archived] == [
        (filename, "🧪 Tester report: group/app !7")]
    assert [(d.chat_id, d.filename) for d in world.telegram.documents] == [
        ("chat-1", filename)]
    assert all("group/app !7" in d.caption for d in world.telegram.documents)

    (entry,) = world.usage_entries()
    tiers = world.settings.llm.tiers
    assert set(entry["models"]) == {tiers.fast.model, tiers.main.model, tiers.smart.model}


def test_investigator_runs_only_for_complex_mrs_that_need_it(world):
    project, mr = _project_with_mr(
        world, files=BILLING_FILES,
        changes=[file_change("billing/refund.py", "-    return 1\n+    return 2\n")])
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.",
                  needs_investigation=True)  # normal complexity -> no investigator

    world.send(mr_webhook(project, mr))

    assert "smart" not in world.llm.tiers()
    assert world.bridge.questions == []
    assert world.telegram.documents == [] and world.bridge.archived == []
    assert len(mr.bot_notes) == 2


def test_failed_investigation_still_delivers_the_review(world):
    project, mr = _project_with_mr(
        world, files=BILLING_FILES,
        changes=[file_change("billing/refund.py", "-    return 1\n+    return 2\n")])
    world.llm.on_complete(triage("complex", needs_investigation=True),
                          "Вердикт: всё в порядке.")
    world.llm.on_agent(AgentScript(final_text="Verdict: fine."),
                       AIError("refusal"))  # e.g. opus-5 cyber safeguards

    world.send(mr_webhook(project, mr))

    assert world.llm.tiers() == ["fast", "main", "smart", "fast"]
    start, review = mr.bot_notes
    assert "Вердикт: всё в порядке." in review
    assert world.telegram.documents == [] and world.telegram.errors == []


# --- 5.3 incremental re-review: delta, same-sha skip, re-review label ---

def test_second_push_reviews_only_the_delta(world):
    project, mr = _project_with_mr(
        world, files={"app.py": "x = 1\n", "util.py": "y = 2\n"},
        changes=[file_change("app.py", "-x = 0\n+x = 1\n")])
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.")
    world.send(mr_webhook(project, mr))
    assert not any(c[0] == "compare" for c in world.gitlab.calls)  # first = full

    # a new push 5 minutes later: only util.py changed since sha-1
    world.clock.advance(300)
    mr.sha = "sha-2"
    mr.change_list.append(file_change("util.py", "-y = 1\n+y = 2\n"))
    project.compare_result = {"diffs": [file_change("util.py", "-y = 1\n+y = 2\n")]}
    _review_round(world, "Verdict: fine.", "Вердикт: дельта в порядке.")

    assert world.send(mr_webhook(project, mr, "update"))["status"] == "accepted"

    assert ("compare", "sha-1", "sha-2") in world.gitlab.calls
    delta_triage, delta_review = world.llm.calls[3:5]
    assert "util.py" in delta_triage.user and "app.py" not in delta_triage.user
    assert reviewed_files(delta_review.user) == ["util.py"]
    assert "INCREMENTAL RE-REVIEW: this MR was already fully reviewed at commit sha-1." \
        in delta_review.system
    assert world.repo.checkouts[-1] == ("group/app", 7, "sha-2")
    assert "Вердикт: дельта в порядке." in mr.bot_notes[-1]


def test_same_sha_after_review_is_skipped_and_re_review_label_forces_full(world):
    project, mr = _project_with_mr(
        world, files={"app.py": "x = 1\n"},
        changes=[file_change("app.py", "-x = 0\n+x = 1\n")])
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.")
    world.send(mr_webhook(project, mr))
    notes_after_first = list(mr.bot_notes)

    # title edit long after the dedupe TTL: same sha -> nothing happens
    world.clock.advance(3600)
    mr.title = "Better title"
    assert world.send(mr_webhook(project, mr, "update"))["status"] == "accepted"
    assert mr.bot_notes == notes_after_first
    assert len(world.llm.calls) == 3
    assert len(world.telegram.messages) == 2  # no new "review started" either

    # the re-review label: same sha, inside the TTL, still a full fresh review
    _review_round(world, "Verdict: fine.", "Вердикт: полное ревью заново.")
    resp = world.send(mr_webhook(project, mr, "update", labels=["re-review"]))

    assert resp["status"] == "accepted"
    full_review = world.llm.calls[4]
    assert "INCREMENTAL RE-REVIEW" not in full_review.system
    assert reviewed_files(full_review.user) == ["app.py"]
    assert not any(c[0] == "compare" for c in world.gitlab.calls)
    assert mr.bot_notes[-2] == RU_START
    assert "Вердикт: полное ревью заново." in mr.bot_notes[-1]


def test_failed_compare_falls_back_to_full_review(world):
    project, mr = _project_with_mr(
        world, files={"app.py": "x = 2\n"},
        changes=[file_change("app.py", "-x = 0\n+x = 2\n")])
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.")
    world.send(mr_webhook(project, mr))

    world.clock.advance(300)
    mr.sha = "sha-2"  # force-push: GitLab can't compare -> empty diffs
    _review_round(world, "Verdict: fine.", "Вердикт: снова всё в порядке.")
    world.send(mr_webhook(project, mr, "update"))

    second_review = world.llm.calls[4]
    assert "INCREMENTAL RE-REVIEW" not in second_review.system
    assert reviewed_files(second_review.user) == ["app.py"]
    assert "Вердикт: снова всё в порядке." in mr.bot_notes[-1]


# --- 5.4 big MR: triage skip_globs and the content budget ladder ---

def test_triage_skip_globs_drop_asset_contents_but_list_them(world):
    icons = {f"public/icons/i{n}.svg": f"<svg>{n}</svg>\n" for n in range(5)}
    changes = [file_change("app.py", "-x = 0\n+x = 1\n")] + [
        file_change(path, f"+{body}", new_file=True) for path, body in icons.items()]
    project, mr = _project_with_mr(
        world, files={"app.py": "x = 1\n", **icons}, changes=changes)
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.",
                  skip_globs=["*.svg"])

    world.send(mr_webhook(project, mr))

    triage_call, review_call = world.llm.calls[:2]
    assert "public/icons/i3.svg" in triage_call.user  # triage sees the manifest
    assert reviewed_files(review_call.user) == ["app.py"]
    assert "<svg>" not in review_call.user
    assert ("SKIPPED — 5 changed file(s) judged to carry no review value; "
            "contents not shown") in review_call.user
    assert "added: public/icons/i3.svg" in review_call.user
    assert not any(c[0] == "file_get" and c[1].endswith(".svg")
                   for c in world.gitlab.calls)


def test_skip_globs_matching_everything_are_ignored(world):
    project, mr = _project_with_mr(
        world, files={"a.py": "a = 1\n", "b.py": "b = 1\n"},
        changes=[file_change("a.py", "+a = 1\n"), file_change("b.py", "+b = 1\n")])
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.",
                  skip_globs=["**", "*.py"])  # catch-all dropped, *.py = 100% -> ignored

    world.send(mr_webhook(project, mr))

    review_call = world.llm.calls[1]
    assert reviewed_files(review_call.user) == ["a.py", "b.py"]
    assert "SKIPPED" not in review_call.user


def test_oversized_mr_degrades_to_a_truncated_diff_instead_of_refusing(world):
    # 3 files x 40k chars = ~60k tokens of diff against a 40k-token budget:
    # full context and diffs-only both overflow, the trimmed subset fits one file
    big = {f"gen/part{n}.py": "+" + f"v{n} = 1  # padding\n" * 2_100 for n in range(3)}
    project, mr = _project_with_mr(
        world, files={path: "" for path in big},
        changes=[file_change(path, diff) for path, diff in big.items()])
    world.configure(llm__max_input_tokens=40_000)
    _review_round(world, "Verdict: fine for the reviewed part.",
                  "Вердикт: в проверенной части всё в порядке.")

    world.send(mr_webhook(project, mr))

    assert [(c.method, c.tier) for c in world.llm.calls] == [
        ("complete_json", "fast"), ("agent_loop", "main"), ("complete", "fast")]
    review_call = world.llm.calls[1]
    assert ("(file context omitted and the diff was truncated — this MR exceeds "
            "the review input budget)") in review_call.user
    assert "--- gen/part0.py ---" in review_call.user
    assert "--- gen/part2.py ---" not in review_call.user
    assert "[2 more changed file(s) omitted" in review_call.user
    # the file-context fetch was skipped up front, not attempted and thrown away
    assert not any(c[0] == "file_get" and c[1].startswith("gen/")
                   for c in world.gitlab.calls)
    assert "Вердикт: в проверенной части всё в порядке." in mr.bot_notes[-1]


# --- 5.5 dialogue: replies in bot threads ---

def _bot_thread(world):
    project, mr = _project_with_mr(
        world, files=BILLING_FILES,
        changes=[file_change("billing/refund.py", "-    return 1\n+    return 2\n")])
    bot_note = mr.add_note("Замечание: refund() не учитывает комиссию.")
    return project, mr, bot_note.discussion_id


def test_dialogue_reply_answers_in_the_bot_thread(world):
    project, mr, thread = _bot_thread(world)
    question = mr.add_note("Где ещё вызывается refund?", author="dev",
                           discussion_id=thread)
    world.llm.on_agent(AgentScript(
        tool_calls=[ToolCall("repo_grep", {"pattern": r"refund\("})],
        final_text="It is also called from billing/api.py:4 (cancel)."))
    world.llm.on_complete("Ещё вызывается из billing/api.py:4 (cancel).")

    resp = world.send(note_webhook(project, mr, question), event="Note Hook")

    assert resp == {"status_code": 200, "status": "accepted", "merge_request": 7,
                    "instance": "primary"}
    assert [(c.method, c.tier) for c in world.llm.calls] == [
        ("agent_loop", "main"), ("complete", "fast")]
    dialogue = world.llm.calls[0]
    assert "Где ещё вызывается refund?" in dialogue.user
    assert "refund() не учитывает комиссию" in dialogue.user  # the whole thread
    (_, grep_out), = dialogue.tool_results
    assert "billing/api.py:4" in grep_out
    assert ("discussion_reply", thread) in world.gitlab.calls
    reply = mr.all_notes[-1]
    assert reply.discussion_id == thread
    assert reply.body == "Ещё вызывается из billing/api.py:4 (cancel)."
    assert world.repo.checkouts == [("group/app", 7, "sha-1")]
    assert len(world.repo.released) == 1
    assert world.telegram.messages == []  # dialogue is GitLab-only
    (entry,) = world.usage_entries()
    assert entry["kind"] == "dialogue"


def test_dialogue_ignores_own_notes_and_foreign_threads(world):
    project, mr, thread = _bot_thread(world)
    own = mr.add_note("Ответ бота", discussion_id=thread)
    human_chat = mr.add_note("Коллеги, созвонимся?", author="dev")

    own_resp = world.send(note_webhook(project, mr, own), event="Note Hook")
    chat_resp = world.send(note_webhook(project, mr, human_chat), event="Note Hook")

    assert own_resp["status"] == "ignored" and own_resp["reason"] == "own note"
    assert chat_resp["status"] == "accepted"  # queued, then not our thread
    assert world.llm.calls == []
    assert [n.body for n in mr.all_notes][-1] == "Коллеги, созвонимся?"


def test_dialogue_no_reply_sentinel_posts_nothing(world):
    project, mr, thread = _bot_thread(world)
    thanks = mr.add_note("Спасибо, поправил.", author="dev", discussion_id=thread)
    world.llm.on_agent(AgentScript(final_text="NO_REPLY"))

    world.send(note_webhook(project, mr, thanks), event="Note Hook")

    assert world.llm.tiers() == ["main"]  # no translation of the sentinel
    assert mr.all_notes[-1] is thanks
    assert not any(c[0] == "discussion_reply" for c in world.gitlab.calls)


def test_dialogue_daily_reply_budget_per_mr(world):
    world.configure(pipeline__dialogue_max_replies_per_mr=1)
    project, mr, thread = _bot_thread(world)
    first = mr.add_note("Почему?", author="dev", discussion_id=thread)
    world.llm.on_agent(AgentScript(final_text="Because the fee is kept."))
    world.llm.on_complete("Потому что комиссия удерживается.")
    world.send(note_webhook(project, mr, first), event="Note Hook")
    assert mr.all_notes[-1].body == "Потому что комиссия удерживается."

    second = mr.add_note("А почему так?", author="dev", discussion_id=thread)
    assert world.send(note_webhook(project, mr, second),
                      event="Note Hook")["status"] == "accepted"

    assert len(world.llm.calls) == 2  # budget spent: no second agent run
    assert mr.all_notes[-1] is second
    # a silent job spent no tokens and leaves no usage entry
    assert [e["kind"] for e in world.usage_entries()] == ["dialogue"]


# --- 5.6 webhook dedupe: burst window and same-sha retries ---

def test_burst_of_events_for_one_mr_reviews_once(world):
    project, mr = _project_with_mr(
        world, files={"app.py": "x = 1\n"},
        changes=[file_change("app.py", "-x = 0\n+x = 1\n")])
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.")

    first = world.send(mr_webhook(project, mr, "reopen"))
    world.clock.advance(5)
    mr.sha = "sha-2"  # reopen + update with a different sha (one user action)
    second = world.send(mr_webhook(project, mr, "update"))

    assert first["status"] == "accepted" and second["status"] == "duplicate"
    assert len(world.llm.calls) == 3
    assert mr.bot_notes.count(RU_START) == 1


def test_webhook_retry_with_same_sha_is_deduped_within_ttl(world):
    project, mr = _project_with_mr(
        world, files={"app.py": "x = 1\n"},
        changes=[file_change("app.py", "-x = 0\n+x = 1\n")])
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.")
    payload = mr_webhook(project, mr)

    world.send(payload)
    world.clock.advance(120)  # past the burst window, inside the 600s TTL
    retry = world.send(payload)

    assert retry["status"] == "duplicate"
    assert len(world.llm.calls) == 3
    assert len(mr.bot_notes) == 2


def test_note_webhooks_dedupe_by_note_id_and_skip_the_burst_window(world):
    project, mr, thread = _bot_thread(world)
    # a review event for the MR opens its burst window...
    _review_round(world, "Verdict: fine.", "Вердикт: всё в порядке.")
    world.send(mr_webhook(project, mr, "update"))
    # ...and a reply right after it is still served
    question = mr.add_note("Почему?", author="dev", discussion_id=thread)
    world.llm.on_agent(AgentScript(final_text="Because."))
    world.llm.on_complete("Потому что.")
    payload = note_webhook(project, mr, question)

    assert world.send(payload, event="Note Hook")["status"] == "accepted"
    assert world.send(payload, event="Note Hook")["status"] == "duplicate"
    assert [n.body for n in mr.all_notes].count("Потому что.") == 1
