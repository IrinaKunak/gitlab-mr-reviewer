"""Review / dialogue use cases and their content helpers, driven over fakes."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from reviewer import ai_client as ai_mod
from reviewer.adapters.gitlab import (
    GitLabVcs,
    parse_merge_request_webhook,
    to_changeset,
)
from reviewer.adapters.notify import NullNotifier
from reviewer.ai_client import AIClient
from reviewer.application import content
from reviewer.domain.models import ChangeSet, FileChange, TriageResult
from tests.factories import (
    INSTANCE,
    dialogue_job,
    make_answer_note,
    make_review_mr,
    make_services,
    make_settings,
    mr_ref,
    review_job,
)

WEBHOOK_PAYLOAD = {
    "object_attributes": {
        "action": "open", "iid": 7, "id": 100,
        "source_branch": "PBV-123-fix-cart", "target_branch": "master",
        "title": "Fix cart", "description": "closes ABC-9",
        "url": "https://lab/x/-/mergerequests/7",
        "last_commit": {"id": "deadbeef"},
    },
    "project": {"id": 1, "path_with_namespace": "g/x", "name": "x"},
    "user": {"username": "max", "name": "Max"},
}


def test_tester_report_targets():
    # owner request 2026-07-23: reports go to the team group(s) too, not only
    # the bridge chat where AIManager archives them (the bridge chat now gets it
    # from the KnowledgeSource, so the Telegram channel skips it)
    from reviewer.bootstrap import build_notifier

    cfg = make_settings(bridge__chat_id="-100bridge")
    tg = cfg.notify.telegram
    tg.enabled, tg.chat_ids, tg.tester_report_chat_ids = True, ["-100team", "-100extra"], []
    (channel,) = build_notifier(cfg).channels
    assert channel.tester_report_chats() == ["-100team", "-100extra"]

    # explicit override narrows the team targets; dedupe against bridge
    tg.tester_report_chat_ids = ["-100team", "-100bridge"]
    assert channel.tester_report_chats() == ["-100team"]


def test_translate_guard_rejects_non_cyrillic_output(monkeypatch):
    # regression: Haiku answered the translate request with English commentary
    # ("you haven't provided a markdown document") and it was posted as the review
    import asyncio
    from types import SimpleNamespace

    answers = iter([
        "I appreciate your message, but you haven't provided a document.",
        "Обзор: всё в порядке.",
    ])

    class StubAI:
        async def complete(self, tier, system, user, **kwargs):
            assert "<document>" in user  # translation input is always wrapped now
            return SimpleNamespace(text=next(answers))

    from reviewer.application.stages import Translator

    t = Translator(StubAI(), "ru")
    # commentary (no Cyrillic) -> deliver the English original instead
    assert asyncio.run(t.translate("review text", "fast")) == "review text"
    # real translation passes through
    assert asyncio.run(t.translate("review text", "fast")) == "Обзор: всё в порядке."


def test_process_skips_merged_or_closed_mr():
    # an update webhook can sit in the queue while the MR gets merged/closed
    # (push fix -> merge on green); the worker must not review it then
    from tests.fakes import FakeGitLab

    for state in ("merged", "closed"):
        gitlab = FakeGitLab()
        gitlab.add_project(1).add_mr(2, changes=[], state=state)
        # the AI is unwired: touching it fails the test
        notifier = NullNotifier()
        svc = make_services(vcs_for=lambda instance: gitlab, notifier=notifier)
        asyncio.run(svc.review_mr.run(review_job()))
        assert notifier.events == []
        assert [c[0] for c in gitlab.calls] == ["mr_get"]  # no notes, no diffs


def _gl_stub(*, project=None, mr=None, **gl_attrs):
    """Just enough python-gitlab surface: gl.projects.get -> project,
    project.mergerequests.get -> mr (lazy or not)."""
    from types import SimpleNamespace
    project = project or SimpleNamespace()
    mr = mr or SimpleNamespace()
    project.mergerequests = SimpleNamespace(get=lambda iid, lazy=False: mr)
    return SimpleNamespace(projects=SimpleNamespace(get=lambda pid, lazy=False: project),
                           **gl_attrs)


def _vcs(gl):
    return GitLabVcs(INSTANCE, client_factory=lambda: gl)


def test_incremental_review_helpers():
    # prompt contract for the anti-pedantry overhaul (dev feedback 2026-07-23)
    from types import SimpleNamespace

    from reviewer import prompts

    assert "## Verdict" in prompts.REVIEW_SYSTEM
    assert "No hypotheticals" in prompts.REVIEW_SYSTEM
    # dev feedback 2026-07-30 (Irina, telemarketing-back !8/!9): four findings in
    # a row were "confirm that a periodic reconciliation exists", about code the
    # reviewer was never shown. Banning second-guessing the author's DECISIONS
    # did not cover asking whether something exists ELSEWHERE — the reviewer
    # cannot see the rest of the repo, so those must be dropped, not hedged.
    assert "ONLY this merge request's changes" in prompts.REVIEW_SYSTEM
    assert "never ask whether such a thing" in prompts.REVIEW_SYSTEM
    for banned in ('"confirm"', '"verify"', '"make sure"', '"double-check"'):
        assert banned in prompts.REVIEW_SYSTEM, banned
    # a docstring explaining WHY is the answer; don't re-ask it
    assert "is the author's" in prompts.REVIEW_SYSTEM
    # and don't ship a finding you yourself called fine
    assert '"looks correct"' in prompts.REVIEW_SYSTEM
    # the investigator DOES have the repo — it must check, not ask
    assert "grep for it and report what you found" in prompts.INVESTIGATOR_SYSTEM
    note = prompts.INCREMENTAL_REVIEW_NOTE.format(prev_sha="abc12345")
    assert "abc12345" in note and "delta" in note
    assert ".ai-review.md" in prompts.guidelines_section("Focus on SQL")

    # delta fetch turns compare diffs into a ChangeSet; degrades to None
    ref = mr_ref()
    stub = SimpleNamespace(
        repository_compare=lambda a, b: {"diffs": [{"new_path": "x.py", "diff": "+1"}]})
    delta = asyncio.run(_vcs(_gl_stub(project=stub)).compare(ref, "aaa", "bbb"))
    assert delta == ChangeSet((FileChange(old_path="", new_path="x.py", diff="+1"),))
    stub_empty = SimpleNamespace(repository_compare=lambda a, b: {"diffs": []})
    assert asyncio.run(_vcs(_gl_stub(project=stub_empty)).compare(ref, "aaa", "bbb")) is None

    def _raise(a, b):
        raise RuntimeError("404 commit not found")
    stub_err = SimpleNamespace(repository_compare=_raise)
    assert asyncio.run(_vcs(_gl_stub(project=stub_err)).compare(ref, "aaa", "bbb")) is None

    # .ai-review.md is best-effort: absent file -> empty string
    class _Files:
        def get(self, path, ref):
            raise RuntimeError("404")
    vcs = _vcs(_gl_stub(project=SimpleNamespace(files=_Files())))
    assert asyncio.run(content.read_guidelines(vcs, ref, "main")) == ""


def test_triage_chooses_skipped_files_and_budget_truncation():
    # MR !779 (655 files, 235k tokens of diff) was refused outright as "MR too
    # large". 439 of those files were SVG/asset blobs — which files are worth
    # reading is a judgement call, so triage makes it; a hardcoded extension
    # list cannot know a project's conventions.
    from reviewer import prompts

    changes = to_changeset({"changes": [
        {"new_path": "src/auth.py", "diff": "+def login():\n" * 50},
        {"new_path": "public/logo.svg", "diff": "+<path d='M0 0'/>\n" * 400},
        {"new_path": "yarn.lock", "diff": "+dep\n" * 300, "new_file": True},
        {"new_path": "src/pay.py", "diff": "+def charge():\n" * 50},
    ]})

    # the manifest triage judges from: status, size and path for every file
    manifest = content.file_manifest(changes)
    assert "modified\t" in manifest and "public/logo.svg" in manifest
    assert "added\t" in manifest                     # yarn.lock is new_file
    assert manifest in prompts.triage_user_prompt(review_job(), "diff", manifest)
    assert "skip_globs" in prompts.TRIAGE_SCHEMA["properties"]
    assert "skip_globs" in prompts.TRIAGE_SYSTEM

    # (resolve_skip's pattern rules and guards: tests/test_domain.py)
    skip = {"public/logo.svg", "yarn.lock"}

    # honouring triage's verdict keeps the code and names (not dumps) the rest
    out = content.extract_diff_only(changes, skip=skip)
    assert "def login" in out and "def charge" in out
    assert "<path d=" not in out
    assert "SKIPPED — 2 changed file(s)" in out
    assert "deleted: " not in out and "modified: public/logo.svg" in out
    # no skip list -> unchanged v1 behaviour, everything included
    assert "<path d=" in content.extract_diff_only(changes)

    # budget cap drops whole files and says so, instead of refusing the MR
    small = content.extract_diff_only(changes, max_chars=800, skip=skip)
    assert len(small) < 2000
    assert "more changed file(s) omitted" in small
    assert "def login" in small                      # first file still reviewed


def test_investigator_degrades_before_cloning(monkeypatch):
    # prod !779: the investigator got the same full context the review had just
    # rejected as too large — and the guard only fires inside agent_loop, AFTER
    # the repo clone, so we paid for a clone then silently dropped the analysis
    cfg = make_settings(llm__max_input_tokens=10_000)
    from reviewer.application.stages import Investigate

    p = Investigate(cfg, AIClient(cfg), bridge=None)
    triage, mr_data = TriageResult(summary="s"), review_job(mr_iid=779)

    huge, small = "x" * 200_000, "y" * 6_000
    # full context too big -> falls back to the diff, no exception, no clone yet
    assert p.content_for(mr_data, huge, triage, "review", small) == small
    # both too big -> a truncated subset, still something to investigate
    picked = p.content_for(mr_data, huge, triage, "review", huge)
    assert 0 < len(picked) < len(huge)
    # fits -> untouched
    assert p.content_for(mr_data, small, triage, "review", "z") == small


def test_translate_long_text_upgrades_tier(monkeypatch):
    # dev feedback 2026-07-23: long reviews came back half-English from Haiku —
    # texts over the threshold must route to the main tier
    import asyncio
    from types import SimpleNamespace

    tiers = []

    class StubAI:
        async def complete(self, tier, system, user, **kwargs):
            tiers.append(tier)
            return SimpleNamespace(text="Перевод готов.")

    from reviewer.application.stages import Translator

    t = Translator(StubAI(), "ru")
    asyncio.run(t.translate("short text", "fast"))
    asyncio.run(t.translate("long text " * 500, "fast"))  # ~5000 chars
    assert tiers == ["fast", "main"]


def test_process_skips_already_reviewed_sha(tmp_path):
    # metadata-only update webhooks (title/labels edits) re-arrive with the same
    # head sha we already reviewed — must skip before any notify/AI spend
    from tests.fakes import FakeGitLab

    gitlab = FakeGitLab()
    gitlab.add_project(1).add_mr(2, changes=[], sha="abc123")
    notifier = NullNotifier()
    svc = make_services(make_settings(tmp_path), vcs_for=lambda instance: gitlab,
                        notifier=notifier)
    job = review_job(last_commit="abc123")
    svc.review_state.set_last_sha(*job.ref.key, "abc123")
    asyncio.run(svc.review_mr.run(job))
    assert notifier.events == []
    assert [c[0] for c in gitlab.calls] == ["mr_get"]


def test_review_content_handles_collapsed_diffs():
    # regression: GitLab returns empty diffs for collapsed (too large) files —
    # exactly the biggest files silently vanished from the review (MR !18)
    from tests.fakes import FakeGitLab

    gitlab = FakeGitLab()
    gitlab.add_project(1, files={"reviewer/ai_client.py": "def core(): ...\n"})
    changes = to_changeset({"changes": [
        {"new_path": "reviewer/ai_client.py", "diff": "", "collapsed": True, "new_file": True},
        {"new_path": "small.py", "diff": "+ok", "new_file": True},
        {"new_path": "unchanged.py", "diff": ""},  # genuinely empty -> still skipped
    ]})
    out = asyncio.run(content.assemble_review_content(gitlab, mr_ref(), changes, "v2"))
    assert "reviewer/ai_client.py" in out and "def core" in out
    assert "DIFF UNAVAILABLE" in out
    assert "unchanged.py" not in out
    assert ("file_get", "reviewer/ai_client.py", "v2") in gitlab.calls

    diff_only = content.extract_diff_only(changes)
    assert "[diff unavailable: file too large]" in diff_only


def test_extract_jira_keys():
    parsed = parse_merge_request_webhook(WEBHOOK_PAYLOAD, INSTANCE)
    keys = content.extract_jira_keys(parsed)
    assert keys == ["PBV-123", "ABC-9"]


def test_format_review_comment_language():
    comment = content.format_review_comment("текст обзора", "ru")
    assert "Автоматический обзор кода" in comment
    assert "Automated Code Review" in content.format_review_comment("review", "en")


def test_thread_helpers():
    from reviewer.domain.models import Discussion, Note

    notes = [Note(1, "reviewer-bot", "## Review\nfinding A"),
             Note(2, "irina", "ну нет изменений же"),
             Note(3, "gitlab", "added 1 commit", system=True)]
    text = content.render_thread(notes, "reviewer-bot")
    assert "[@reviewer-bot [bot — this is you]]" in text
    assert "[@irina]" in text and "added 1 commit" not in text

    assert content.thread_involves_bot(notes, "reviewer-bot") is True
    assert content.thread_involves_bot(notes, "other-bot") is False
    assert content.thread_involves_bot(notes, "") is False
    # bot note id 1 < trigger id 2 -> not answered yet; a bot note after -> answered
    assert content.bot_answered_after(notes, 2, "reviewer-bot") is False
    answered = [*notes, Note(4, "reviewer-bot", "ok")]
    assert content.bot_answered_after(answered, 2, "reviewer-bot") is True

    assert content.mentions_user("cc @Reviewer-Bot, взгляни", "reviewer-bot") is True
    assert content.mentions_user("no mention here", "reviewer-bot") is False
    assert content.mentions_user("@reviewer-bot2 hi", "reviewer-bot") is False
    assert content.mentions_user("hi", "") is False

    # adapter find_discussion: hint path, scan fallback, API failure -> None
    from types import SimpleNamespace
    hit = SimpleNamespace(id="d9", attributes={"notes": [{"id": 5, "body": "x"}]})

    class _Discussions:
        def get(self, did, lazy=False):
            assert did == "d9"
            return hit

        def list(self, **kw):
            return iter([SimpleNamespace(id="other", attributes={"notes": [{"id": 1}]}),
                         hit])
    vcs = _vcs(_gl_stub(mr=SimpleNamespace(discussions=_Discussions())))
    expected = Discussion("d9", (Note(5, "", "x"),))
    assert asyncio.run(vcs.find_discussion(mr_ref(), 5, "d9")) == expected
    assert asyncio.run(vcs.find_discussion(mr_ref(), 5, "")) == expected

    class _Broken:
        def get(self, did, lazy=False):
            raise RuntimeError("403")

        def list(self, **kw):
            raise RuntimeError("403")
    broken = _vcs(_gl_stub(mr=SimpleNamespace(discussions=_Broken())))
    assert asyncio.run(broken.find_discussion(mr_ref(), 5, "d9")) is None


def test_dialogue_answers_in_thread(tmp_path):
    # "Пусть сам подтверждает" — the bot answers a dev's reply, checking the
    # repo itself; NO_REPLY suppresses the answer; budget caps runaway threads
    from reviewer.ai_client import AIResult
    from tests.fakes import FakeGitLab, file_change

    gitlab = FakeGitLab()
    mr = gitlab.add_project(1, "g/p").add_mr(
        10, changes=[file_change("a.py", "+x = 1")], title="MR 10", author="dev1",
        source_branch="f", target_branch="dev")
    finding = mr.add_note("finding")                                    # the bot
    question = mr.add_note("точно?", author="irina", discussion_id=finding.discussion_id)

    class _NoRepo:
        async def checkout_mr(self, *a, **kw):
            raise RuntimeError("clone disabled in tests")

    answers = iter([AIResult(text="Checked views.py:12 — IsAuthenticated is intact."),
                    AIResult(text="NO_REPLY")])
    seen_prompts: list[str] = []

    class StubAI:
        async def agent_loop(self, tier, system, user, tools, **kw):
            assert tier == "main"
            seen_prompts.append(user)
            return next(answers)

    cfg = make_settings(tmp_path, pipeline__language="en")
    p = make_answer_note(cfg, ai=StubAI(), repo_cache=_NoRepo(),
                      vcs_for=lambda instance: gitlab)
    note = dialogue_job(project_id=1, project_path="g/p", mr_iid=10, note_id=question.id,
                        discussion_id=finding.discussion_id, note_body="точно?",
                        note_author="irina", last_commit="sha1")
    asyncio.run(p.execute(note))
    assert mr.bot_notes == ["finding", "Checked views.py:12 — IsAuthenticated is intact."]
    assert ("discussion_reply", finding.discussion_id) in gitlab.calls
    # the model sees the thread, knows which side it is, and the diff
    assert "[bot — this is you]" in seen_prompts[0]
    assert "Answer the last message, from @irina." in seen_prompts[0]
    assert "+x = 1" in seen_prompts[0]

    # a newer question in the same thread; NO_REPLY -> nothing posted
    again = mr.add_note("ещё?", author="irina", discussion_id=finding.discussion_id)
    asyncio.run(p.execute(replace(note, note_id=again.id, note_body="ещё?")))
    assert len(mr.bot_notes) == 2

    # the bot's own note must never trigger an answer (loop guard)
    asyncio.run(p.execute(replace(note, note_id=4, note_author="reviewer-bot")))
    assert len(mr.bot_notes) == 2

    # a thread without the bot and without a mention is the humans talking
    chat = mr.add_note("hi", author="artem")
    asyncio.run(p.execute(replace(note, note_id=chat.id, note_author="artem",
                                       discussion_id=chat.discussion_id, note_body="hi")))
    assert len(mr.bot_notes) == 2

    # per-MR budget: once exhausted the bot stays silent
    cfg.pipeline.dialogue_max_replies_per_mr = 1
    assert p.budget.allows(("primary", 1, 10)) is False
    assert p.budget.allows(("primary", 1, 11)) is True


def test_review_with_tools_verifies_and_falls_back(monkeypatch):
    # the review stage checks its own cross-file concerns with repo tools;
    # any tool-path failure degrades to the plain single-shot review
    from reviewer.ai_client import AIError, AIResult

    mr_data = review_job(mr_iid=1, author="dev1", source_branch="f", target_branch="dev")
    triage = TriageResult()
    calls: list[str] = []

    class StubAI:
        def __init__(self, agent_result=None, agent_exc=None):
            self.agent_result, self.agent_exc = agent_result, agent_exc

        async def agent_loop(self, tier, system, user, tools, **kw):
            calls.append("agent")
            assert tier == "main"
            assert [t.name for t in tools] == ["repo_find_symbol", "repo_grep",
                                               "repo_read_file", "repo_list_tree"]
            assert "REPO ACCESS FOR THIS REVIEW" in system
            if self.agent_exc:
                raise self.agent_exc
            return self.agent_result

        async def complete(self, tier, system, user, **kw):
            calls.append("complete")
            return AIResult(text="## Verdict\n**SHIP** plain path")

    from pathlib import Path

    from reviewer.application.stages import Review
    from reviewer.repo_cache import repo_tools

    cfg, tools = make_settings(), repo_tools(Path("."))

    # tool path succeeds -> plain completion never runs
    p = Review(cfg, StubAI(agent_result=AIResult(text="## Verdict\n**SHIP** verified")))
    out = asyncio.run(p.review(mr_data, "content", triage, "diff", "", None, tools=tools))
    assert out.text == "## Verdict\n**SHIP** verified" and calls == ["agent"]
    assert out.tool_assisted

    # loop dies (refusal, provider trouble) -> plain review still ships
    calls.clear()
    p = Review(cfg, StubAI(agent_exc=AIError("boom")))
    out = asyncio.run(p.review(mr_data, "content", triage, "diff", "", None, tools=tools))
    assert "plain path" in out.text and calls == ["agent", "complete"]

    # loop ran out of turns mid-check (no verdict) -> plain review
    calls.clear()
    p = Review(cfg, StubAI(agent_result=AIResult(text="hmm, checking")))
    out = asyncio.run(p.review(mr_data, "content", triage, "diff", "", None, tools=tools))
    assert "plain path" in out.text and calls == ["agent", "complete"]

    # no worktree (checkout failed / flag off) -> straight to the plain path
    calls.clear()
    p = Review(cfg, StubAI())
    out = asyncio.run(p.review(mr_data, "content", triage, "diff", "", None, tools=None))
    assert calls == ["complete"]


def test_dialogue_and_tools_prompt_contract():
    from reviewer import prompts

    note = prompts.REVIEW_TOOLS_NOTE.format(max_calls=8)
    assert "REPO ACCESS FOR THIS REVIEW" in note
    assert "about 8 tool calls" in note
    # the upgrade of the epistemic rule: unverifiable -> CHECK it, not drop it
    assert "CHECK it yourself" in note
    assert "confirm what these tools can" in note

    assert "NO_REPLY" in prompts.DIALOGUE_SYSTEM
    assert "CHECK, don't ask" in prompts.DIALOGUE_SYSTEM
    assert "cannot approve, merge, or modify" in prompts.DIALOGUE_SYSTEM
    user = prompts.dialogue_user_prompt("hdr", "thread-text", "irina",
                                        position="a.py:5", diff="+d")
    assert "thread-text" in user and "a.py:5" in user
    assert user.rstrip().endswith("Answer the last message, from @irina.")


_LEAKY = "connect to http://10.0.0.5:8080/internal failed, see /srv/app/secrets.py"


@pytest.mark.parametrize("exc_factory", [
    lambda: RuntimeError(_LEAKY),
    lambda: ai_mod.AIError(_LEAKY),
])
def test_pipeline_error_note_hides_exception_text(monkeypatch, exc_factory):

    from reviewer.application.review_mr import ReviewMergeRequest

    notes = []

    async def fake_inner(self, job, tracker=None):
        raise exc_factory()

    async def fake_note(self, ref, body):
        notes.append(body)

    monkeypatch.setattr(ReviewMergeRequest, "run", fake_inner)
    monkeypatch.setattr(ReviewMergeRequest, "_safe_note", fake_note)

    notifier = NullNotifier()
    p = make_review_mr(notifier=notifier)
    asyncio.run(p.execute(review_job(job_id="deadbeef")))
    assert len(notes) == 1
    assert "10.0.0.5" not in notes[0] and "/srv/app" not in notes[0]
    assert "deadbeef" in notes[0]
    # details stay available internally
    (failed,) = notifier.events
    assert _LEAKY in failed.details and failed.job_id == "deadbeef"


def test_deliver_review_failure_note_hides_exception_text():
    from types import SimpleNamespace

    posted = []

    async def fake_post(ref, body):
        if not posted:
            posted.append(None)
            raise RuntimeError(_LEAKY)
        posted.append(body)

    from reviewer.application.stages import Deliver, ReviewContext
    from reviewer.domain.models import ChangeSet, MergeRequestInfo

    vcs = SimpleNamespace(post_note=fake_post)
    deliver = Deliver(make_settings(), NullNotifier())
    ctx = ReviewContext(job=review_job(job_id="cafe0001"), vcs=vcs, changes=ChangeSet(),
                        mr=MergeRequestInfo("opened", "t", "dev", "f", "main"),
                        review_out="review")
    assert asyncio.run(deliver.run(ctx)).posted is False
    assert "10.0.0.5" not in posted[1] and "cafe0001" in posted[1]
