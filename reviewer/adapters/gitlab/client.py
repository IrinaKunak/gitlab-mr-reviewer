"""GitLab implementation of VcsPort (python-gitlab, sync, run in threads).

One client per instance, built once by bootstrap: the HTTP session (and its
connection pool) is reused, and `gl.auth()` runs once at startup instead of on
every job (#12). python-gitlab objects never leave this module — callers get
domain models.

SOCKS proxying uses a requests `proxies` dict (socks5h://) instead of v1's
global socket monkey-patch — no process-wide side effects.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

import gitlab
import requests
from gitlab.exceptions import GitlabError, GitlabGetError

from ...application.ports import VcsError, VcsNotFound
from ...domain.models import (
    ChangeSet,
    DiffRefs,
    Discussion,
    FileChange,
    InstanceRef,
    MergeRequestInfo,
    MergeRequestRef,
    Note,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# GitLab's default page size. Larger pages are not safe: GitLab 17.5 answers
# /diffs?per_page=50 (and 100) with a 500 on ordinary MRs (checked 2026-10-08)
DIFFS_PER_PAGE = 20
MAX_NOTES = 100  # first page of comments, oldest first
MAX_DISCUSSION_SCAN = 300  # fallback when the payload carries no discussion_id
# merge_status values that mean "cannot merge as is"
CONFLICT_STATUSES = ("cannot_be_merged", "cannot_be_merged_recheck")


def to_changeset(raw: Any) -> ChangeSet:
    """GitLab `changes` / `diffs` / `compare` payload -> ChangeSet.

    Accepts the MR-changes dict ({"changes": [...]}) or a bare diff list."""
    items = raw.get("changes", []) if isinstance(raw, dict) else (raw or [])
    return ChangeSet(tuple(
        FileChange(
            old_path=c.get("old_path") or "",
            new_path=c.get("new_path") or "",
            diff=c.get("diff") or "",
            new_file=bool(c.get("new_file")),
            deleted_file=bool(c.get("deleted_file")),
            renamed_file=bool(c.get("renamed_file")),
            collapsed=bool(c.get("collapsed") or c.get("too_large")),
        ) for c in items if isinstance(c, dict)))


def has_conflicts(attrs: dict) -> bool:
    """Unmergeable as is: conflicts, a failed mergeability check, or unresolved
    blocking discussions (v1 semantics)."""
    return bool(attrs.get("merge_status", "") in CONFLICT_STATUSES
                or attrs.get("has_conflicts", False)
                or attrs.get("blocking_discussions_resolved", True) is False)


def _username(user: Any) -> str:
    if isinstance(user, dict):
        return user.get("username") or ""
    return getattr(user, "username", "") or ""


def _note(raw: dict) -> Note:
    return Note(id=int(raw.get("id") or 0), author=_username(raw.get("author")),
                body=raw.get("body") or "", system=bool(raw.get("system")))


def _vcs_errors(func: Callable[..., T]) -> Callable[..., T]:
    """python-gitlab exceptions -> VcsError / VcsNotFound at the port boundary."""
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return func(*args, **kwargs)
        except GitlabGetError as exc:
            if getattr(exc, "response_code", None) == 404:
                raise VcsNotFound(str(exc)) from exc
            raise VcsError(str(exc)) from exc
        except GitlabError as exc:
            raise VcsError(str(exc)) from exc
        except requests.RequestException as exc:
            raise VcsError(str(exc)) from exc
    return wrapper


class GitLabVcs:
    """VcsPort for one GitLab instance."""

    def __init__(self, instance: InstanceRef, proxies: dict | None = None,
                 client_factory: Callable[[], Any] | None = None) -> None:
        self.instance = instance
        self._proxies = proxies
        self._client_factory = client_factory or self._new_client
        self._gl: Any = None
        self._bot_username = ""

    # --- client ---

    def _new_client(self) -> gitlab.Gitlab:
        session = requests.Session()
        if self._proxies:
            session.proxies = self._proxies
        return gitlab.Gitlab(self.instance.url, private_token=self.instance.token,
                             session=session)

    @property
    def gl(self) -> Any:
        if self._gl is None:
            self._gl = self._client_factory()
        return self._gl

    @property
    def bot_username(self) -> str:
        return self._bot_username

    async def connect(self) -> str:
        """Startup check: authenticate once, learn the bot's own username."""
        await asyncio.to_thread(self._auth)
        return self._bot_username

    @_vcs_errors
    def _auth(self) -> None:
        self.gl.auth()
        self._bot_username = _username(getattr(self.gl, "user", None))

    def _mr(self, ref: MergeRequestRef) -> Any:
        """Lazy handles: notes/discussions/uploads need no GET of the MR first."""
        project = self.gl.projects.get(ref.project_id, lazy=True)
        return project, project.mergerequests.get(ref.mr_iid, lazy=True)

    # --- reads ---

    async def get_merge_request(self, ref: MergeRequestRef) -> MergeRequestInfo:
        return await asyncio.to_thread(self._get_merge_request, ref)

    @_vcs_errors
    def _get_merge_request(self, ref: MergeRequestRef) -> MergeRequestInfo:
        project = self.gl.projects.get(ref.project_id, lazy=True)
        mr = project.mergerequests.get(ref.mr_iid)
        attrs = getattr(mr, "attributes", None) or {}
        refs = attrs.get("diff_refs") or {}
        return MergeRequestInfo(
            state=attrs.get("state") or "opened",
            title=attrs.get("title") or "",
            author=_username(attrs.get("author")),
            source_branch=attrs.get("source_branch") or "",
            target_branch=attrs.get("target_branch") or "",
            sha=attrs.get("sha") or "",
            has_conflicts=has_conflicts(attrs),
            diff_refs=DiffRefs(refs.get("base_sha") or "", refs.get("head_sha") or "",
                               refs.get("start_sha") or "") if refs else None)

    async def get_changes(self, ref: MergeRequestRef) -> ChangeSet:
        return await asyncio.to_thread(self._get_changes, ref)

    @_vcs_errors
    def _get_changes(self, ref: MergeRequestRef) -> ChangeSet:
        """GET /merge_requests/:iid/diffs, all pages (/changes is deprecated
        since GitLab 15.7). /diffs has no `access_raw_diffs`, so files it
        returns collapsed are re-fetched once through /changes with
        access_raw_diffs=true, which reads them from Gitaly past the per-file
        collapse limit. Whatever is still collapsed stays marked collapsed —
        the content builder shows its current text instead (never dropped).
        If /diffs itself fails (it 500s on some page sizes), the whole MR is
        read the old way, so the new endpoint can never cost a review."""
        path = f"/projects/{ref.project_id}/merge_requests/{ref.mr_iid}/diffs"
        _, mr = self._mr(ref)
        try:
            diffs = self.gl.http_list(path, query_data={"per_page": DIFFS_PER_PAGE},
                                      get_all=True)
        except GitlabError as exc:
            logger.warning("/diffs for !%s failed (%s) — reading /changes instead",
                           ref.mr_iid, exc)
            return to_changeset(mr.changes(access_raw_diffs="true"))
        changes = to_changeset(list(diffs))
        if not any(f.collapsed and not f.diff for f in changes.files):
            return changes
        try:
            raw = to_changeset(mr.changes(access_raw_diffs="true"))
        except GitlabError as exc:
            logger.warning("raw diffs for collapsed files of !%s unavailable: %s",
                           ref.mr_iid, exc)
            return changes
        by_path = {f.path: f for f in raw.files if f.diff}
        return ChangeSet(tuple(
            by_path.get(f.path, f) if f.collapsed and not f.diff else f
            for f in changes.files))

    async def compare(self, ref: MergeRequestRef, from_sha: str,
                      to_sha: str) -> ChangeSet | None:
        try:
            return await asyncio.to_thread(self._compare, ref, from_sha, to_sha)
        except Exception as exc:  # noqa: BLE001 — degrade to a full review
            logger.warning("compare %s..%s failed (%s) — falling back to full review",
                           from_sha[:8], to_sha[:8], exc)
            return None

    def _compare(self, ref: MergeRequestRef, from_sha: str, to_sha: str) -> ChangeSet | None:
        project = self.gl.projects.get(ref.project_id, lazy=True)
        comp = project.repository_compare(from_sha, to_sha)
        diffs = comp.get("diffs") if isinstance(comp, dict) else getattr(comp, "diffs", None)
        return to_changeset(diffs) if diffs else None

    async def read_file(self, ref: MergeRequestRef, path: str, git_ref: str) -> str:
        return await asyncio.to_thread(self._read_file, ref, path, git_ref)

    @_vcs_errors
    def _read_file(self, ref: MergeRequestRef, path: str, git_ref: str) -> str:
        project = self.gl.projects.get(ref.project_id, lazy=True)
        return project.files.get(path, ref=git_ref).decode().decode("utf-8", errors="replace")

    async def list_notes(self, ref: MergeRequestRef) -> list[Note]:
        return await asyncio.to_thread(self._list_notes, ref)

    @_vcs_errors
    def _list_notes(self, ref: MergeRequestRef) -> list[Note]:
        _, mr = self._mr(ref)
        notes = mr.notes.list(per_page=MAX_NOTES, order_by="created_at", sort="asc",
                              get_all=False)
        return [_note(getattr(n, "attributes", None) or {}) for n in notes]

    async def find_discussion(self, ref: MergeRequestRef, note_id: int | None,
                              discussion_id: str = "") -> Discussion | None:
        try:
            return await asyncio.to_thread(self._find_discussion, ref, note_id, discussion_id)
        except Exception as exc:  # noqa: BLE001 — dialogue is best-effort
            logger.warning("could not fetch discussion for note %s: %s", note_id, exc)
            return None

    def _find_discussion(self, ref: MergeRequestRef, note_id: int | None,
                         discussion_id: str) -> Discussion | None:
        def notes_of(disc: Any) -> tuple[Note, ...]:
            raw = (getattr(disc, "attributes", None) or {}).get("notes") or []
            return tuple(_note(n) for n in raw)
        _, mr = self._mr(ref)
        if discussion_id:
            notes = notes_of(mr.discussions.get(discussion_id))
            if notes:
                return Discussion(discussion_id, notes)
        for idx, disc in enumerate(mr.discussions.list(iterator=True)):
            if idx >= MAX_DISCUSSION_SCAN:
                break
            notes = notes_of(disc)
            if any(n.id == note_id for n in notes):
                return Discussion(str(getattr(disc, "id", "")), notes)
        return None

    # --- writes ---

    async def post_note(self, ref: MergeRequestRef, body: str) -> None:
        await asyncio.to_thread(self._post_note, ref, body)

    @_vcs_errors
    def _post_note(self, ref: MergeRequestRef, body: str) -> None:
        _, mr = self._mr(ref)
        mr.notes.create({"body": body})

    async def reply_in_discussion(self, ref: MergeRequestRef, discussion_id: str,
                                  body: str) -> None:
        """On a standalone (non-thread) note GitLab converts it into a thread —
        same endpoint either way."""
        await asyncio.to_thread(self._reply, ref, discussion_id, body)

    @_vcs_errors
    def _reply(self, ref: MergeRequestRef, discussion_id: str, body: str) -> None:
        _, mr = self._mr(ref)
        mr.discussions.get(discussion_id, lazy=True).notes.create({"body": body})

    async def upload(self, ref: MergeRequestRef, filename: str, content: bytes) -> str | None:
        try:
            upload = await asyncio.to_thread(self._upload, ref, filename, content)
        except Exception as exc:  # noqa: BLE001 — delivery degradation, not fatal
            logger.error("upload of %s failed: %s", filename, exc)
            return None
        return upload.get("markdown") or upload.get("url")

    def _upload(self, ref: MergeRequestRef, filename: str, content: bytes) -> dict:
        project = self.gl.projects.get(ref.project_id, lazy=True)
        return project.upload(filename, content)
