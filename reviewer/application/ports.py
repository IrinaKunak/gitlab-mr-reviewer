"""Ports: what the review application needs from the outside world.

The pipeline talks to these protocols only; adapters (adapters/gitlab) and
test fakes (tests/fakes) implement them. No SDK type crosses a port — every
argument and result is a domain model or a plain value.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Protocol

from ..domain.events import NotificationEvent
from ..domain.models import (
    ChangeSet,
    Discussion,
    Job,
    MergeRequestInfo,
    MergeRequestRef,
    Note,
)

if TYPE_CHECKING:
    from ..ai_client import ToolDef


class VcsError(Exception):
    """The VCS API failed (network, auth, 5xx). Message is for logs/alerts only —
    never post it to an MR (it can carry internal URLs)."""


class VcsNotFound(VcsError):
    """Project, MR, file or discussion does not exist (or is not visible)."""


class VcsPort(Protocol):
    """One VCS instance (a GitLab server). Methods are keyed by MergeRequestRef."""

    async def connect(self) -> str:
        """Authenticate once; returns (and remembers) the bot's own username."""
        ...

    @property
    def bot_username(self) -> str:
        """The bot's login on this instance ("" until known)."""
        ...

    async def get_merge_request(self, ref: MergeRequestRef) -> MergeRequestInfo: ...

    async def get_changes(self, ref: MergeRequestRef) -> ChangeSet:
        """Every changed file of the MR. Files whose diff the server withholds
        come back `collapsed` with an empty diff — never silently dropped."""
        ...

    async def compare(self, ref: MergeRequestRef, from_sha: str,
                      to_sha: str) -> ChangeSet | None:
        """Changes between two commits (incremental re-review); None when they
        can't be compared (force-push, GC'd sha) — the caller reviews in full."""
        ...

    async def read_file(self, ref: MergeRequestRef, path: str, git_ref: str) -> str:
        """File content at a branch/sha; raises VcsNotFound / VcsError."""
        ...

    async def list_notes(self, ref: MergeRequestRef) -> list[Note]:
        """MR comments, oldest first (bounded to the first page)."""
        ...

    async def find_discussion(self, ref: MergeRequestRef, note_id: int | None,
                              discussion_id: str = "") -> Discussion | None:
        """The thread containing note_id (by discussion_id when known)."""
        ...

    async def post_note(self, ref: MergeRequestRef, body: str) -> None: ...

    async def reply_in_discussion(self, ref: MergeRequestRef, discussion_id: str,
                                  body: str) -> None: ...

    async def upload(self, ref: MergeRequestRef, filename: str, content: bytes) -> str | None:
        """Attach a file to the project; the markdown link, or None on failure."""
        ...


class RepoWorkspace(Protocol):
    """A checkout of the MR head with read-only repo tools over it.

    One session per review (shared by the tool-assisted review and the
    investigator) or per dialogue reply; the context manager releases the
    worktree on exit. It yields None when the checkout failed — callers
    degrade to working without tools, the review still runs."""

    def session(self, ref: MergeRequestRef,
                sha: str | None) -> AbstractAsyncContextManager[list[ToolDef] | None]: ...


class Notifier(Protocol):
    """A notification channel (Telegram; Bitrix24 later).

    Contract: `notify` never raises (fail-open, like usage accounting); the
    channel decides which events it reacts to, formats them itself and splits
    long messages to its own limits."""

    name: str

    async def notify(self, event: NotificationEvent) -> None: ...


class KnowledgeSource(Protocol):
    """Company knowledge the investigator can ask (AIManager over the Review
    Bridge). Independent of the notification channels."""

    enabled: bool

    async def ask(self, question: str) -> str | None:
        """The answer, or None (timeout / not found / disabled); never raises."""
        ...

    async def archive(self, filename: str, content: bytes, caption: str = "") -> None:
        """Hand a document to the knowledge base (tester reports); never raises."""
        ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class JobQueue(Protocol):
    """Durable queue of accepted jobs (survives restarts)."""

    def enqueue(self, job: Job) -> None: ...

    def claim(self) -> Job | None:
        """The next job (now running), or None when empty."""
        ...

    def complete(self, job_id: str) -> None: ...

    def fail(self, job_id: str, error: str) -> None:
        """Retry while attempts are left, else give up."""
        ...

    def release(self, job_id: str) -> None:
        """Back to the queue without counting the attempt (graceful shutdown)."""
        ...

    def recover(self) -> tuple[int, int]:
        """Startup: re-queue jobs a dead process left running -> (requeued, failed)."""
        ...
