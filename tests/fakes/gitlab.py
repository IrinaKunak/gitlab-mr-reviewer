"""In-memory GitLab implementing VcsPort (reviewer/application/ports.py).

The pipeline talks to it exactly as it talks to adapters/gitlab.GitLabVcs.
What the real adapter does with python-gitlab (pagination, collapsed-diff
refetch, error mapping) is covered by its own tests in test_unit.py. Every
API-shaped call is appended to `FakeGitLab.calls` so tests can assert what
was (not) touched.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from reviewer.adapters.gitlab import to_changeset
from reviewer.application.ports import VcsNotFound
from reviewer.domain.models import (
    ChangeSet,
    Discussion,
    MergeRequestInfo,
    MergeRequestRef,
    Note,
)

CONFLICT_STATUSES = ("cannot_be_merged", "cannot_be_merged_recheck")


@dataclass
class FakeNote:
    id: int
    body: str
    author: dict
    system: bool = False
    discussion_id: str = ""

    def as_note(self) -> Note:
        return Note(self.id, self.author["username"], self.body, self.system)


class FakeMR:
    def __init__(self, gl: FakeGitLab, project: FakeProject, iid: int, *,
                 changes: list[dict], title: str, author: str, sha: str,
                 source_branch: str, target_branch: str, state: str,
                 merge_status: str, has_conflicts: bool):
        self._gl = gl
        self.project_id = project.id
        self.iid = iid
        self.id = project.id * 1000 + iid
        self.title = title
        self.author = {"username": author}
        self.sha = sha
        self.source_branch = source_branch
        self.target_branch = target_branch
        self.state = state
        self.merge_status = merge_status
        self.has_conflicts = has_conflicts
        self.change_list = changes
        self.all_notes: list[FakeNote] = []

    def add_note(self, body: str, *, author: str | None = None, discussion_id: str = "",
                 system: bool = False, call: tuple | None = None) -> FakeNote:
        """Post a note; without discussion_id it starts a new thread (as in GitLab)."""
        note_id = next(self._gl.ids)
        note = FakeNote(note_id, body, {"username": author or self._gl.bot_username},
                        system=system, discussion_id=discussion_id or f"d{note_id}")
        self.all_notes.append(note)
        if call:
            self._gl.calls.append(call)
        return note

    def thread(self, discussion_id: str) -> tuple[Note, ...]:
        return tuple(n.as_note() for n in self.all_notes if n.discussion_id == discussion_id)

    @property
    def bot_notes(self) -> list[str]:
        """Bodies the bot posted, in order (top-level notes and thread replies)."""
        return [n.body for n in self.all_notes
                if n.author["username"] == self._gl.bot_username]


class FakeProject:
    def __init__(self, gl: FakeGitLab, project_id: int, path: str,
                 files: dict[str, str], default_branch: str):
        self._gl = gl
        self.id = project_id
        self.path_with_namespace = path
        self.default_branch = default_branch
        self.repo_files = files
        self.mrs: dict[int, FakeMR] = {}
        self.uploads: list[tuple[str, bytes]] = []
        self.compare_result: dict | None = None

    def add_mr(self, iid: int, *, changes: list[dict], title: str = "Change things",
               author: str = "dev", sha: str = "sha-1", source_branch: str = "feature",
               target_branch: str = "main", state: str = "opened",
               merge_status: str = "can_be_merged", has_conflicts: bool = False) -> FakeMR:
        mr = FakeMR(self._gl, self, iid, changes=changes, title=title, author=author,
                    sha=sha, source_branch=source_branch, target_branch=target_branch,
                    state=state, merge_status=merge_status, has_conflicts=has_conflicts)
        self.mrs[iid] = mr
        return mr


@dataclass
class FakeGitLab:
    bot_username: str = "reviewer-bot"
    projects_by_id: dict[int, FakeProject] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    ids: itertools.count = field(default_factory=lambda: itertools.count(100))

    def add_project(self, project_id: int = 1, path: str = "group/app", *,
                    files: dict[str, str] | None = None,
                    default_branch: str = "main") -> FakeProject:
        project = FakeProject(self, project_id, path, files or {}, default_branch)
        self.projects_by_id[project_id] = project
        return project

    # --- lookup ---

    def _project(self, ref: MergeRequestRef) -> FakeProject:
        if ref.project_id not in self.projects_by_id:
            raise VcsNotFound(f"404 Project {ref.project_id} Not Found")
        return self.projects_by_id[ref.project_id]

    def _mr(self, ref: MergeRequestRef) -> FakeMR:
        project = self._project(ref)
        if ref.mr_iid not in project.mrs:
            raise VcsNotFound(f"404 Merge Request {ref.mr_iid} Not Found")
        return project.mrs[ref.mr_iid]

    # --- VcsPort ---

    async def connect(self) -> str:
        self.calls.append(("auth",))
        return self.bot_username

    async def get_merge_request(self, ref: MergeRequestRef) -> MergeRequestInfo:
        self.calls.append(("mr_get", ref.mr_iid))
        mr = self._mr(ref)
        return MergeRequestInfo(
            state=mr.state, title=mr.title, author=mr.author["username"],
            source_branch=mr.source_branch, target_branch=mr.target_branch, sha=mr.sha,
            has_conflicts=mr.merge_status in CONFLICT_STATUSES or mr.has_conflicts)

    async def get_changes(self, ref: MergeRequestRef) -> ChangeSet:
        self.calls.append(("mr_changes", ref.mr_iid))
        return to_changeset([dict(c) for c in self._mr(ref).change_list])

    async def compare(self, ref: MergeRequestRef, from_sha: str,
                      to_sha: str) -> ChangeSet | None:
        self.calls.append(("compare", from_sha, to_sha))
        diffs = (self._project(ref).compare_result or {}).get("diffs")
        return to_changeset(diffs) if diffs else None

    async def read_file(self, ref: MergeRequestRef, path: str, git_ref: str) -> str:
        self.calls.append(("file_get", path, git_ref))
        files = self._project(ref).repo_files
        if path not in files:
            raise VcsNotFound(f"404 File {path} Not Found")
        return files[path]

    async def list_notes(self, ref: MergeRequestRef) -> list[Note]:
        self.calls.append(("notes_list", ref.mr_iid))
        return [n.as_note() for n in self._mr(ref).all_notes]

    async def find_discussion(self, ref: MergeRequestRef, note_id: int | None,
                              discussion_id: str = "") -> Discussion | None:
        mr = self._mr(ref)
        if discussion_id and mr.thread(discussion_id):
            return Discussion(discussion_id, mr.thread(discussion_id))
        for note in mr.all_notes:
            if note.id == note_id:
                return Discussion(note.discussion_id, mr.thread(note.discussion_id))
        return None

    async def post_note(self, ref: MergeRequestRef, body: str) -> None:
        self._mr(ref).add_note(body, call=("note_create", ref.mr_iid))

    async def reply_in_discussion(self, ref: MergeRequestRef, discussion_id: str,
                                  body: str) -> None:
        mr = self._mr(ref)
        if not mr.thread(discussion_id):
            raise VcsNotFound(f"404 Discussion {discussion_id} Not Found")
        mr.add_note(body, discussion_id=discussion_id,
                    call=("discussion_reply", discussion_id))

    async def upload(self, ref: MergeRequestRef, filename: str, content: bytes) -> str | None:
        self.calls.append(("upload", filename))
        project = self._project(ref)
        project.uploads.append((filename, content))
        url = f"/uploads/{len(project.uploads)}/{filename}"
        return f"[{filename}]({url})"


def mr_webhook(project: FakeProject, mr: FakeMR, action: str = "open", *,
               actor: str | None = None, labels: list[str] | None = None,
               description: str = "") -> dict:
    """GitLab 'Merge Request Hook' payload for the fake MR's current state."""
    return {
        "object_kind": "merge_request",
        "user": {"username": actor or mr.author["username"]},
        "project": {"id": project.id, "path_with_namespace": project.path_with_namespace},
        "object_attributes": {
            "iid": mr.iid, "id": mr.id, "action": action, "title": mr.title,
            "description": description, "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "url": f"https://gitlab.test/{project.path_with_namespace}/-/merge_requests/{mr.iid}",
            "last_commit": {"id": mr.sha},
        },
        "labels": [{"title": t} for t in (labels or [])],
    }


def note_webhook(project: FakeProject, mr: FakeMR, note: FakeNote) -> dict:
    """GitLab 'Note Hook' payload for a comment already added to the fake MR."""
    return {
        "object_kind": "note",
        "user": {"username": note.author["username"]},
        "project": {"id": project.id, "path_with_namespace": project.path_with_namespace},
        "object_attributes": {
            "id": note.id, "note": note.body, "noteable_type": "MergeRequest",
            "discussion_id": note.discussion_id, "system": note.system,
        },
        "merge_request": {
            "iid": mr.iid, "last_commit": {"id": mr.sha},
            "url": f"https://gitlab.test/{project.path_with_namespace}/-/merge_requests/{mr.iid}",
        },
    }


def file_change(path: str, diff: str, *, new_file: bool = False) -> dict:
    return {"old_path": path, "new_path": path, "diff": diff, "new_file": new_file,
            "deleted_file": False, "renamed_file": False}
