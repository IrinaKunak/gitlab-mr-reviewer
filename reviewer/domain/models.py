"""Domain models: what flows through the review pipeline.

Replaces the untyped `mr_data` / `gitlab_config` / `triage` / `changes` /
`investigation` dicts (#6): a typo in a field name is now an AttributeError at
the first test run instead of a silent `.get()` default in production.
Everything here is frozen and I/O-free; adapters (adapters/gitlab, server) build
these from API payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar


class Tier(StrEnum):
    """Model tier. A StrEnum, so it compares and hashes like the plain strings
    the config, overrides file and usage log already use."""
    FAST = "fast"  # triage, trivial review, short translation
    MAIN = "main"  # standard review, dialogue, long translation
    SMART = "smart"  # investigator


class Complexity(StrEnum):
    TRIVIAL = "trivial"
    NORMAL = "normal"
    COMPLEX = "complex"


class JobKind(StrEnum):
    REVIEW = "review"
    DIALOGUE = "dialogue"  # answer to a developer's comment (Note Hook)


@dataclass(frozen=True)
class InstanceRef:
    """A configured GitLab instance. `name` is part of the review-state key."""
    name: str
    url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class MergeRequestRef:
    instance: InstanceRef
    project_id: int
    project_path: str
    mr_iid: int
    url: str = ""

    @property
    def key(self) -> tuple[str, int, int]:
        """(instance, project, iid) — dedupe, review state and reply budgets."""
        return (self.instance.name, self.project_id, self.mr_iid)


@dataclass(frozen=True)
class DiffRefs:
    """The MR's diff anchor SHAs (needed for line-anchored discussions)."""
    base_sha: str
    head_sha: str
    start_sha: str


@dataclass(frozen=True)
class MergeRequestInfo:
    """Live MR state as the VCS reports it when a job runs (the webhook payload
    can be stale: the MR may have been merged or relabelled since)."""
    state: str
    title: str
    author: str  # the real author — the webhook's "user" is the event actor
    source_branch: str
    target_branch: str
    sha: str = ""
    has_conflicts: bool = False
    diff_refs: DiffRefs | None = None


@dataclass(frozen=True)
class Note:
    id: int
    author: str
    body: str
    system: bool = False


@dataclass(frozen=True)
class Discussion:
    id: str
    notes: tuple[Note, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Job:
    """A queued unit of work; `job_id` ties its log lines, alerts and MR notes."""
    kind: ClassVar[JobKind]
    ref: MergeRequestRef
    last_commit: str | None = None
    job_id: str = ""


@dataclass(frozen=True, kw_only=True)
class ReviewJob(Job):
    kind: ClassVar[JobKind] = JobKind.REVIEW
    title: str
    source_branch: str
    target_branch: str
    author: str  # webhook actor until the pipeline relabels it with the real author
    description: str = ""
    action: str = ""
    mr_id: int | None = None
    # [re-review] marker / re-review label: full fresh review, bypasses dedupe
    force_full: bool = False


@dataclass(frozen=True, kw_only=True)
class DialogueJob(Job):
    kind: ClassVar[JobKind] = JobKind.DIALOGUE
    note_id: int | None
    note_body: str
    note_author: str
    discussion_id: str = ""
    note_position: str = ""  # "path:line" for diff comments


@dataclass(frozen=True)
class FileChange:
    old_path: str
    new_path: str
    diff: str = ""
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False
    collapsed: bool = False  # GitLab withheld the diff (collapsed / too_large)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path

    @property
    def status(self) -> str:
        if self.new_file:
            return "added"
        if self.deleted_file:
            return "deleted"
        if self.renamed_file:
            return "renamed"
        return "modified"

    @property
    def readable(self) -> bool:
        """Has something to show: a diff, or a collapsed one to fall back from."""
        return bool(self.diff or self.collapsed)


@dataclass(frozen=True)
class ChangeSet:
    files: tuple[FileChange, ...] = ()

    def __len__(self) -> int:
        return len(self.files)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


def _str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str | int | float))


@dataclass(frozen=True)
class TriageResult:
    complexity: Complexity = Complexity.NORMAL
    needs_investigation: bool = False
    risk_areas: tuple[str, ...] = ()
    jira_keys: tuple[str, ...] = ()
    summary: str = ""
    skip_globs: tuple[str, ...] = ()

    @classmethod
    def from_model(cls, parsed: dict, extra_jira_keys: tuple[str, ...] = ()) -> TriageResult:
        """Normalize the fast tier's JSON. Lenient on purpose: an unknown
        complexity behaves as "normal" (as the string compare did), non-list
        fields read as empty, and regex-found Jira keys are merged in."""
        try:
            complexity = Complexity(str(parsed.get("complexity")))
        except ValueError:
            complexity = Complexity.NORMAL
        keys = list(_str_list(parsed.get("jira_keys")))
        keys += [k for k in extra_jira_keys if k not in keys]
        globs = parsed.get("skip_globs")
        return cls(
            complexity=complexity,
            needs_investigation=bool(parsed.get("needs_investigation")),
            risk_areas=_str_list(parsed.get("risk_areas")),
            jira_keys=tuple(keys),
            summary=str(parsed.get("summary") or ""),
            # raw strings kept; resolve_skip applies its own guards
            skip_globs=tuple(g for g in globs if isinstance(g, str))
            if isinstance(globs, list) else (),
        )


@dataclass(frozen=True)
class ReviewResult:
    text: str
    tool_assisted: bool = False


@dataclass(frozen=True)
class Investigation:
    full_text: str
    impact: str
    tester_report: str | None = None
