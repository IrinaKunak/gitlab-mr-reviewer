"""Domain objects with test defaults: override only what a test is about."""

from __future__ import annotations

from typing import Any

from reviewer.domain.models import DialogueJob, InstanceRef, MergeRequestRef, ReviewJob

INSTANCE = InstanceRef("primary", "https://gitlab.test", "t")

_REF_FIELDS = ("instance", "project_id", "project_path", "mr_iid", "url")


def mr_ref(**kw: Any) -> MergeRequestRef:
    values: dict[str, Any] = {"instance": INSTANCE, "project_id": 1,
                              "project_path": "group/proj", "mr_iid": 2,
                              "url": "https://gitlab.test/group/proj/-/merge_requests/2"}
    values.update(kw)
    return MergeRequestRef(**values)


def _split(kw: dict[str, Any]) -> tuple[MergeRequestRef, dict[str, Any]]:
    ref_kw = {k: kw.pop(k) for k in _REF_FIELDS if k in kw}
    return kw.pop("ref", None) or mr_ref(**ref_kw), kw


def review_job(**kw: Any) -> ReviewJob:
    ref, rest = _split(dict(kw))
    values: dict[str, Any] = {"title": "t", "source_branch": "feature",
                              "target_branch": "main", "author": "dev"}
    values.update(rest)
    return ReviewJob(ref=ref, **values)


def dialogue_job(**kw: Any) -> DialogueJob:
    ref, rest = _split(dict(kw))
    values: dict[str, Any] = {"note_id": 1, "note_body": "why?", "note_author": "dev"}
    values.update(rest)
    return DialogueJob(ref=ref, **values)
