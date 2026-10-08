"""Unit tests for the review stages (application/stages) and the repo session
(CacheWorkspace): each stage driven on its own over a ReviewContext."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from reviewer.adapters.gitlab import to_changeset
from reviewer.ai_client import AIError, AIResult
from reviewer.application.stages import (
    Deliver,
    DeliverTesterReport,
    Investigate,
    ReviewContext,
    Translate,
    Translator,
    Triage,
)
from reviewer.domain.models import (
    ChangeSet,
    Complexity,
    Investigation,
    MergeRequestInfo,
    TriageResult,
)
from reviewer.repo_cache import CacheWorkspace
from tests.factories import make_settings, mr_ref, review_job
from tests.fakes import FakeGitLab, FakeTelegram, file_change


def _ctx(gitlab=None, **kw) -> ReviewContext:
    gitlab = gitlab or FakeGitLab()
    values = dict(job=review_job(mr_iid=2, job_id="job00001"), vcs=gitlab,
                  mr=MergeRequestInfo("opened", "t", "dev", "f", "main"),
                  changes=ChangeSet())
    values.update(kw)
    return ReviewContext(**values)


class _AI:
    """complete / complete_json answers from a list; records tiers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.tiers: list[str] = []

    async def complete(self, tier, system, user, **kw):
        self.tiers.append(tier)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return AIResult(text=answer)

    async def complete_json(self, tier, system, user, schema, **kw):
        self.tiers.append(tier)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_triage_stage_resolves_skips_and_falls_back():
    changes = to_changeset([file_change("app.py", "+x"), file_change("logo.svg", "+<svg>")])
    ai = _AI({"complexity": "complex", "needs_investigation": True,
              "skip_globs": ["*.svg"], "jira_keys": []})
    ctx = asyncio.run(Triage(ai).run(_ctx(changes=changes)))
    assert ctx.triage.complexity is Complexity.COMPLEX and ctx.triage.needs_investigation
    assert ctx.skip == {"logo.svg"}

    # the fast tier failing never blocks the review: normal, keys from the branch
    job = review_job(source_branch="feature/PAY-12-x")
    ctx = asyncio.run(Triage(_AI(AIError("down"))).run(_ctx(job=job, changes=changes)))
    assert ctx.triage.complexity is Complexity.NORMAL
    assert ctx.triage.jira_keys == ("PAY-12",) and ctx.skip == frozenset()


def test_translate_stage_only_for_ru():
    ai = _AI("Перевод.")
    ctx = asyncio.run(Translate(Translator(ai, "ru")).run(_ctx(review_en="text")))
    assert ctx.review_out == "Перевод." and ai.tiers == ["fast"]

    ai = _AI()  # en: no AI call at all
    ctx = asyncio.run(Translate(Translator(ai, "en")).run(_ctx(review_en="text")))
    assert ctx.review_out == "text" and ai.tiers == []


def test_investigate_stage_appends_impact_and_survives_failure():
    class AgentAI:
        def __init__(self, result):
            self.result = result

        def guard_input_size(self, *parts):
            pass

        async def agent_loop(self, tier, system, user, tools, **kw):
            assert tier == "smart"
            assert [t.name for t in tools] == []  # no checkout, bridge off
            if isinstance(self.result, Exception):
                raise self.result
            return AIResult(text=self.result)

    cfg = make_settings()
    bridge = SimpleNamespace(enabled=False)
    text = ("## Impact analysis\nTouches billing.\n\n## Tester report\nCheck the refund flow.")
    ctx = asyncio.run(Investigate(cfg, AgentAI(text), bridge).run(
        _ctx(review_en="review", review_content="diff")))
    assert ctx.investigation is not None
    assert ctx.review_en.startswith("review\n\n---\n\n")

    ctx = asyncio.run(Investigate(cfg, AgentAI(AIError("refusal")), bridge).run(
        _ctx(review_en="review", review_content="diff")))
    assert ctx.investigation is None and ctx.review_en == "review"


def test_deliver_stage_posts_and_notifies():
    gitlab = FakeGitLab()
    mr = gitlab.add_project(1).add_mr(2, changes=[])
    telegram = FakeTelegram()
    cfg = make_settings(pipeline__language="en")
    cfg.notify.telegram.enabled = True
    ctx = asyncio.run(Deliver(cfg, telegram).run(_ctx(gitlab, review_out="LGTM")))
    assert ctx.posted is True
    assert mr.bot_notes[0].startswith("## 🤖") and "LGTM" in mr.bot_notes[0]
    assert len(telegram.notifications) == 1 and "LGTM" in telegram.notifications[0]


def test_deliver_tester_report_inlines_when_upload_fails():
    gitlab = FakeGitLab()
    mr = gitlab.add_project(1).add_mr(2, changes=[])

    async def no_upload(ref, filename, content):
        return None
    gitlab.upload = no_upload  # type: ignore[method-assign]

    telegram = FakeTelegram()
    cfg = make_settings(pipeline__language="en", bridge__chat_id="bridge")
    cfg.pipeline.stages.tester_report = True
    stage = DeliverTesterReport(cfg, telegram, Translator(_AI(), "en"))
    inv = Investigation(full_text="x", impact="", tester_report="Steps: 1. open")
    asyncio.run(stage.run(_ctx(gitlab, investigation=inv)))
    assert mr.bot_notes == ["Steps: 1. open"]
    assert [d.chat_id for d in telegram.documents][0] == "bridge"

    # flag off -> nothing delivered
    cfg.pipeline.stages.tester_report = False
    asyncio.run(stage.run(_ctx(gitlab, investigation=inv)))
    assert mr.bot_notes == ["Steps: 1. open"]


def test_cache_workspace_session(tmp_path):
    released: list[Path] = []

    class Cache:
        def __init__(self, fail=False):
            self.fail = fail

        async def checkout_mr(self, instance, project_path, mr_iid, sha):
            if self.fail:
                raise RuntimeError("clone failed")
            return tmp_path

        async def release(self, worktree):
            released.append(worktree)

    async def use(cache):
        async with CacheWorkspace(cache).session(mr_ref(), "sha") as tools:
            return tools

    tools = asyncio.run(use(Cache()))
    assert [t.name for t in tools] == ["repo_find_symbol", "repo_grep",
                                       "repo_read_file", "repo_list_tree"]
    assert released == [tmp_path]  # released on exit

    assert asyncio.run(use(Cache(fail=True))) is None  # degrade, never raise
    assert released == [tmp_path]


def test_triage_result_default_is_normal():
    assert TriageResult().complexity is Complexity.NORMAL
