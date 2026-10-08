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


# --- object graph -------------------------------------------------------------

class _Unwired:
    """A dependency the test did not expect to be used: any access fails loudly."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        raise AssertionError(f"test touched unwired {self._name}.{attr}")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"test called unwired {self._name}")


def make_settings(tmp_path: Any = None, **values: Any) -> Any:
    """Settings() from the pinned test env; dotted overrides via `__`
    (pipeline__language="ru"); state/log/cache dirs under tmp_path when given."""
    from reviewer.config import Settings
    cfg = Settings()
    if tmp_path is not None:
        cfg.storage.state_dir = str(tmp_path / "state")
        cfg.storage.log_dir = str(tmp_path / "logs")
        cfg.storage.ai_cache_dir = str(tmp_path / "cache")
    for name, value in values.items():
        *parents, leaf = name.split("__")
        target = cfg
        for part in parents:
            target = getattr(target, part)
        assert leaf in type(target).model_fields, f"unknown setting {name}"
        setattr(target, leaf, value)
    return cfg


def make_services(cfg: Any = None, **deps: Any) -> Any:
    """bootstrap.build_services with inert fakes on every outer edge the test
    does not pass explicitly (a touched unwired edge fails the test)."""
    from reviewer.bootstrap import build_services
    from tests.fakes import FakeBridge, FakeTelegram
    cfg = cfg if cfg is not None else make_settings()
    deps.setdefault("telegram", FakeTelegram())
    deps.setdefault("ai", _Unwired("ai"))
    deps.setdefault("bridge", FakeBridge(enabled=False))
    deps.setdefault("repo_cache", _Unwired("repo_cache"))
    deps.setdefault("vcs_for", _Unwired("vcs_for"))
    deps.setdefault("workers", 0)
    return build_services(cfg, **deps)


def make_review_mr(cfg: Any = None, **deps: Any) -> Any:
    return make_services(cfg, **deps).review_mr


def make_answer_note(cfg: Any = None, **deps: Any) -> Any:
    return make_services(cfg, **deps).answer_note
