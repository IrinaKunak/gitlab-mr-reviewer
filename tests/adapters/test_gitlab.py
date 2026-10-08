"""GitLab adapter (VcsPort over python-gitlab) and webhook parsing."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from reviewer.adapters.gitlab import (
    GitLabVcs,
    parse_merge_request_webhook,
    parse_note_webhook,
)
from reviewer.application import content
from reviewer.server import ReviewQueue
from tests.factories import (
    INSTANCE,
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


def test_force_full_re_review_marker():
    # a re-review label / [re-review] title marker forces a fresh full review
    # (regenerates the tester report on demand) and bypasses webhook dedupe,
    # since the label-add event carries the same sha the TTL window swallows

    payload = {
        "object_attributes": {
            "action": "update", "iid": 5, "id": 50, "title": "INCR-54",
            "source_branch": "b", "target_branch": "master",
            "url": "https://x/-/merge_requests/5",
            "last_commit": {"id": "abc"}},
        "project": {"id": 170, "path_with_namespace": "g/p"},
        "user": {"username": "dev"},
        "labels": [{"title": "re-review"}],
    }
    parsed = parse_merge_request_webhook(payload, INSTANCE)
    assert parsed and parsed.force_full is True
    payload["labels"] = []
    assert parse_merge_request_webhook(payload, INSTANCE).force_full is False
    payload["object_attributes"]["title"] = "INCR-54 [re-review]"
    assert parse_merge_request_webhook(payload, INSTANCE).force_full is True

    q = ReviewQueue(workers=1, dedupe_ttl=600, burst_window=30)
    job = review_job(project_id=170, mr_iid=5, last_commit="abc")
    assert q.submit(job) is True
    assert q.submit(job) is False                                # normal dedupe
    assert q.submit(replace(job, force_full=True)) is True       # forced through


def test_real_mr_author_and_comment_fetch():
    # webhook "user" is the EVENT ACTOR (title edit by the owner relabeled other
    # people's MRs as spikerwork) — the live MR carries the real author
    from types import SimpleNamespace

    from gitlab.exceptions import GitlabListError

    from reviewer.application.ports import VcsError
    from reviewer.domain.models import Note

    ref = mr_ref()
    live = SimpleNamespace(attributes={
        "state": "opened", "title": "t", "author": {"username": "nisvem"},
        "source_branch": "f", "target_branch": "main", "sha": "abc",
        "merge_status": "can_be_merged",
        "diff_refs": {"base_sha": "b", "head_sha": "abc", "start_sha": "s"}})
    info = asyncio.run(_vcs(_gl_stub(mr=live)).get_merge_request(ref))
    assert info.author == "nisvem" and info.sha == "abc" and not info.has_conflicts
    assert info.diff_refs.head_sha == "abc"
    no_author = SimpleNamespace(attributes={"state": "opened", "author": None})
    assert asyncio.run(_vcs(_gl_stub(mr=no_author)).get_merge_request(ref)).author == ""

    def note(author, body, system=False):
        return SimpleNamespace(attributes={"id": 1, "author": {"username": author},
                                           "body": body, "system": system})

    raw = [
        note("gitlab", "added 1 commit", system=True),      # system -> skipped
        note("botuser", "## 🤖 Automated Code Review ..."),  # our own -> skipped
        note("irina", "это осознанное изменение, фабрика исключений"),
        note("artem", "каталог без бэка не бывает"),
    ]
    stub_mr = SimpleNamespace(notes=SimpleNamespace(list=lambda **kw: raw))
    notes = asyncio.run(_vcs(_gl_stub(mr=stub_mr)).list_notes(ref))
    assert notes[2] == Note(1, "irina", "это осознанное изменение, фабрика исключений")
    text = content.format_comments(notes, bot_username="botuser")
    assert "[irina]: это осознанное" in text
    assert "[artem]:" in text
    assert "Automated Code Review" not in text and "added 1 commit" not in text

    # oversized discussions keep the tail (latest replies)
    long_notes = [Note(i, "dev", f"comment {i} " + "x" * 500) for i in range(30)]
    capped = content.format_comments(long_notes, max_chars=2000)
    assert len(capped) <= 2001 and "comment 29" in capped

    # API failures surface as VcsError (the pipeline treats comments as optional)
    def _raise(**kw):
        raise GitlabListError("403 Forbidden", response_code=403)
    broken = SimpleNamespace(notes=SimpleNamespace(list=_raise))
    with pytest.raises(VcsError):
        asyncio.run(_vcs(_gl_stub(mr=broken)).list_notes(ref))


def test_gitlab_diffs_paginated_and_collapsed_files_refetched_raw():
    # #17: /changes is deprecated since GitLab 15.7 — read /diffs, all pages.
    # /diffs has no access_raw_diffs, so ONLY files it returns collapsed are
    # re-read via /changes?access_raw_diffs=true (Gitaly, past the per-file
    # collapse limit); still-collapsed ones stay marked (content shows the file)
    from types import SimpleNamespace

    listed = []

    def http_list(path, query_data=None, get_all=False):
        listed.append((path, query_data, get_all))
        return [{"new_path": "a.py", "diff": "+a"},
                {"new_path": "big.py", "diff": "", "collapsed": True},
                {"new_path": "huge.bin", "diff": "", "too_large": True}]

    raw_calls = []

    def changes(**kw):
        raw_calls.append(kw)
        return {"changes": [{"new_path": "a.py", "diff": "+a"},
                            {"new_path": "big.py", "diff": "+raw big diff"},
                            {"new_path": "huge.bin", "diff": ""}]}

    gl = _gl_stub(mr=SimpleNamespace(changes=changes), http_list=http_list)
    got = asyncio.run(_vcs(gl).get_changes(mr_ref(project_id=7, mr_iid=3)))
    assert listed == [("/projects/7/merge_requests/3/diffs", {"per_page": 20}, True)]
    assert raw_calls == [{"access_raw_diffs": "true"}]
    by_path = {f.path: f for f in got.files}
    assert by_path["big.py"].diff == "+raw big diff"
    assert by_path["huge.bin"].collapsed and not by_path["huge.bin"].diff
    assert [f.path for f in got.files] == ["a.py", "big.py", "huge.bin"]  # order kept

    # nothing collapsed -> /diffs alone, the deprecated endpoint is never called
    raw_calls.clear()
    gl2 = _gl_stub(mr=SimpleNamespace(changes=changes),
                   http_list=lambda *a, **kw: [{"new_path": "a.py", "diff": "+a"}])
    assert len(asyncio.run(_vcs(gl2).get_changes(mr_ref()))) == 1
    assert raw_calls == []

    # GitLab 17.5 answers /diffs with a 500 on some page sizes (prod 2026-10-08):
    # a failing /diffs falls back to the old read instead of failing the review
    from gitlab.exceptions import GitlabListError

    def broken_list(*a, **kw):
        raise GitlabListError("500 Internal Server Error", response_code=500)
    gl3 = _gl_stub(mr=SimpleNamespace(changes=changes), http_list=broken_list)
    assert len(asyncio.run(_vcs(gl3).get_changes(mr_ref()))) == 3
    assert raw_calls == [{"access_raw_diffs": "true"}]


def test_gitlab_client_authenticates_once_and_maps_errors():
    # #12: one client per instance, gl.auth() once (startup), not per job
    from types import SimpleNamespace

    from gitlab.exceptions import GitlabCreateError, GitlabGetError

    from reviewer.application.ports import VcsError, VcsNotFound

    built, auths = [], []

    def factory():
        gl = _gl_stub(user=None)
        gl.auth = lambda: (auths.append(1), setattr(gl, "user",
                                                    SimpleNamespace(username="bot")))
        built.append(gl)
        return gl

    vcs = GitLabVcs(INSTANCE, client_factory=factory)
    assert vcs.bot_username == ""
    assert asyncio.run(vcs.connect()) == "bot" and vcs.bot_username == "bot"
    assert vcs.gl is vcs.gl  # reused
    assert len(built) == 1 and len(auths) == 1

    def missing(iid, lazy=False):
        raise GitlabGetError("404 Not found", response_code=404)
    gone = _gl_stub()
    gone.projects.get(1).mergerequests.get = missing
    with pytest.raises(VcsNotFound):
        asyncio.run(_vcs(gone).get_merge_request(mr_ref()))

    def refuse(data):
        raise GitlabCreateError("500 boom", response_code=500)
    gl = _gl_stub(mr=SimpleNamespace(notes=SimpleNamespace(create=refuse)))
    with pytest.raises(VcsError):
        asyncio.run(_vcs(gl).post_note(mr_ref(), "x"))

    # conflicts come with the MR itself — no second request (v1 called http_get)
    conflicted = SimpleNamespace(attributes={"state": "opened",
                                             "merge_status": "cannot_be_merged"})
    assert asyncio.run(_vcs(_gl_stub(mr=conflicted)).get_merge_request(
        mr_ref())).has_conflicts
    unresolved = SimpleNamespace(attributes={"state": "opened",
                                             "blocking_discussions_resolved": False})
    assert asyncio.run(_vcs(_gl_stub(mr=unresolved)).get_merge_request(
        mr_ref())).has_conflicts


def test_parse_webhook_url_fix_and_actions():
    parsed = parse_merge_request_webhook(WEBHOOK_PAYLOAD, INSTANCE)
    assert parsed is not None
    assert parsed.ref.url == "https://lab/x/-/merge_requests/7"  # contractual URL typo fix
    assert parsed.last_commit == "deadbeef"
    assert parsed.ref.instance is INSTANCE

    closed = {**WEBHOOK_PAYLOAD,
              "object_attributes": {**WEBHOOK_PAYLOAD["object_attributes"], "action": "close"}}
    assert parse_merge_request_webhook(closed, INSTANCE) is None


def test_parse_webhook_no_review_marker():
    tagged = {**WEBHOOK_PAYLOAD,
              "object_attributes": {**WEBHOOK_PAYLOAD["object_attributes"],
                                    "title": "big infra change [no-review]"}}
    assert parse_merge_request_webhook(tagged, INSTANCE) is None

    labeled = {**WEBHOOK_PAYLOAD, "labels": [{"title": "No-Review"}]}
    assert parse_merge_request_webhook(labeled, INSTANCE) is None


def test_parse_note_webhook_variants():
    # "жаль, он диалоги не поддерживает" — replies to the bot's review comment
    # arrive as Note Hook events; only human MR comments become dialogue jobs
    base = {
        "object_kind": "note",
        "user": {"username": "irina"},
        "project": {"id": 42, "path_with_namespace": "novacard/telemarketing-back"},
        "object_attributes": {
            "id": 555, "note": "а точно IsAuthenticated остался?",
            "noteable_type": "MergeRequest", "system": False,
            "discussion_id": "abc123", "position": None,
        },
        "merge_request": {"iid": 10, "url": "https://x/mr/10",
                          "last_commit": {"id": "sha1"}},
    }
    parsed = parse_note_webhook(base, INSTANCE)
    assert parsed and parsed.kind == "dialogue"
    assert parsed.ref.mr_iid == 10 and parsed.note_id == 555
    assert parsed.discussion_id == "abc123"
    assert parsed.note_author == "irina"
    assert parsed.last_commit == "sha1"

    # a comment on a diff line carries its anchor
    diff_note = {**base, "object_attributes": {
        **base["object_attributes"],
        "position": {"new_path": "app/views.py", "new_line": 88}}}
    assert parse_note_webhook(diff_note, INSTANCE).note_position == "app/views.py:88"

    # system notes, non-MR comments, empty bodies -> not dialogue material
    system_note = {**base, "object_attributes": {**base["object_attributes"], "system": True}}
    assert parse_note_webhook(system_note, INSTANCE) is None
    issue_note = {**base, "object_attributes": {
        **base["object_attributes"], "noteable_type": "Issue"}}
    assert parse_note_webhook(issue_note, INSTANCE) is None
    empty = {**base, "object_attributes": {**base["object_attributes"], "note": "  "}}
    assert parse_note_webhook(empty, INSTANCE) is None
    assert parse_note_webhook({**base, "merge_request": {}}, INSTANCE) is None
    assert parse_note_webhook({"object_kind": "push"}, INSTANCE) is None
