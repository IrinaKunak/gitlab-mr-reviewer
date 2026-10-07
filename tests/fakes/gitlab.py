"""In-memory GitLab behind the python-gitlab object model.

Replaces only `gitlab_io.get_gitlab_client`: everything above it (content
assembly, conflict check, note posting, discussion lookup) runs the real
gitlab_io code against these objects. Every API-shaped call is appended to
`FakeGitLab.calls` so tests can assert what was (not) touched.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from gitlab.exceptions import GitlabGetError


def _not_found(what: str) -> GitlabGetError:
    return GitlabGetError(f"404 {what} Not Found", response_code=404)


@dataclass
class FakeNote:
    id: int
    body: str
    author: dict
    system: bool = False
    discussion_id: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "body": self.body, "author": self.author,
                "system": self.system}


class FakeDiscussion:
    def __init__(self, mr: FakeMR, disc_id: str):
        self._mr = mr
        self.id = disc_id
        self.notes = SimpleNamespace(create=self._create)

    @property
    def attributes(self) -> dict:
        return {"id": self.id,
                "notes": [n.as_dict() for n in self._mr.all_notes
                          if n.discussion_id == self.id]}

    def _create(self, data: dict) -> FakeNote:
        return self._mr.add_note(data["body"], discussion_id=self.id,
                                 call=("discussion_reply", self.id))


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
        self.manager = SimpleNamespace(gitlab=gl)
        self.notes = SimpleNamespace(create=self._create_note, list=self._list_notes)
        self.discussions = SimpleNamespace(get=self._get_discussion,
                                           list=self._list_discussions)

    # --- python-gitlab surface ---

    def changes(self, **kwargs) -> dict[str, Any]:
        self._gl.calls.append(("mr_changes", self.iid, kwargs))
        return {"changes": [dict(c) for c in self.change_list]}

    def _create_note(self, data: dict) -> FakeNote:
        return self.add_note(data["body"], call=("note_create", self.iid))

    def _list_notes(self, **kwargs) -> list[FakeNote]:
        self._gl.calls.append(("notes_list", self.iid))
        return list(self.all_notes)

    def _get_discussion(self, disc_id: str) -> FakeDiscussion:
        if not any(n.discussion_id == disc_id for n in self.all_notes):
            raise _not_found(f"Discussion {disc_id}")
        return FakeDiscussion(self, disc_id)

    def _list_discussions(self, **kwargs) -> list[FakeDiscussion]:
        ids = dict.fromkeys(n.discussion_id for n in self.all_notes)
        return [FakeDiscussion(self, d) for d in ids]

    # --- test helpers ---

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
        self.mergerequests = SimpleNamespace(get=self._get_mr)
        self.files = SimpleNamespace(get=self._get_file)

    def _get_mr(self, iid: int) -> FakeMR:
        self._gl.calls.append(("mr_get", iid))
        if iid not in self.mrs:
            raise _not_found(f"Merge Request {iid}")
        return self.mrs[iid]

    def _get_file(self, path: str, ref: str = "") -> SimpleNamespace:
        self._gl.calls.append(("file_get", path, ref))
        if path not in self.repo_files:
            raise _not_found(f"File {path}")
        content = self.repo_files[path].encode()
        return SimpleNamespace(decode=lambda: content)

    def repository_compare(self, from_sha: str, to_sha: str) -> dict:
        self._gl.calls.append(("compare", from_sha, to_sha))
        return self.compare_result or {"diffs": []}

    def upload(self, filename: str, content: bytes) -> dict:
        self._gl.calls.append(("upload", filename))
        self.uploads.append((filename, content))
        url = f"/uploads/{len(self.uploads)}/{filename}"
        return {"url": url, "markdown": f"[{filename}]({url})"}

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

    def __post_init__(self):
        self.projects = SimpleNamespace(get=self._get_project)
        self.user = SimpleNamespace(username=self.bot_username)

    def client(self, gitlab_config: dict) -> FakeGitLab:
        """Drop-in for gitlab_io.get_gitlab_client."""
        self.calls.append(("auth", gitlab_config.get("name")))
        return self

    def _get_project(self, project_id: int) -> FakeProject:
        self.calls.append(("project_get", project_id))
        if project_id not in self.projects_by_id:
            raise _not_found(f"Project {project_id}")
        return self.projects_by_id[project_id]

    def http_get(self, path: str) -> dict:
        """Raw MR details (used by the conflict check)."""
        self.calls.append(("http_get", path))
        _, pid, _, iid = path.strip("/").split("/")
        mr = self.projects_by_id[int(pid)].mrs[int(iid)]
        return {"merge_status": mr.merge_status, "has_conflicts": mr.has_conflicts}

    def add_project(self, project_id: int = 1, path: str = "group/app", *,
                    files: dict[str, str] | None = None,
                    default_branch: str = "main") -> FakeProject:
        project = FakeProject(self, project_id, path, files or {}, default_branch)
        self.projects_by_id[project_id] = project
        return project


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


def file_change(path: str, diff: str, *, new_file: bool = False) -> dict:
    return {"old_path": path, "new_path": path, "diff": diff, "new_file": new_file,
            "deleted_file": False, "renamed_file": False}
