"""GitLab API layer. Ported from v1 w-server.py.

Change vs v1: SOCKS proxying uses a requests `proxies` dict (socks5h://) instead
of the global socket.socket monkey-patch — no process-wide side effects.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import gitlab
import requests

from .config import settings
from .domain.models import (
    ChangeSet,
    DialogueJob,
    FileChange,
    InstanceRef,
    MergeRequestRef,
    ReviewJob,
)

logger = logging.getLogger(__name__)

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")

MAX_LISTED_SKIPPED_FILES = 40
MAX_MANIFEST_FILES = 600


def to_changeset(raw: Any) -> ChangeSet:
    """GitLab `changes` / `compare` payload -> ChangeSet.

    Accepts the MR-changes dict ({"changes": [...]}) or a bare diff list
    (repository_compare's "diffs")."""
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


def file_manifest(changes: ChangeSet) -> str:
    """Path + status + diff size for every changed file.

    Cheap enough to hand the triage model so IT decides which files carry no
    review value (assets, generated output, vendored code) — a hardcoded
    extension list can't know a given project's conventions."""
    rows = [f"{f.status}\t{len(f.diff)}\t{f.path or 'unknown'}"
            for f in changes.files[:MAX_MANIFEST_FILES]]
    total = len(changes)
    if total > MAX_MANIFEST_FILES:
        rows.append(f"... and {total - MAX_MANIFEST_FILES} more files")
    return "\n".join(rows)


def summarize_skipped(entries: list[str]) -> str:
    """One compact block naming files whose contents were deliberately skipped."""
    if not entries:
        return ""
    shown = entries[:MAX_LISTED_SKIPPED_FILES]
    extra = len(entries) - len(shown)
    body = "\n".join(f"  {e}" for e in shown)
    if extra:
        body += f"\n  ... and {extra} more"
    return (f"\n{'=' * 80}\nSKIPPED — {len(entries)} changed file(s) judged to carry "
            f"no review value; contents not shown\n{'=' * 80}\n{body}\n")


def get_gitlab_client(instance: InstanceRef) -> gitlab.Gitlab:
    session = requests.Session()
    proxies = settings.network.requests_proxies
    if proxies:
        session.proxies = proxies
    gl = gitlab.Gitlab(instance.url, private_token=instance.token, session=session)
    gl.auth()
    return gl


def parse_merge_request_webhook(payload: dict[str, Any],
                                instance: InstanceRef) -> ReviewJob | None:
    """Parse the MR webhook payload. Contract preserved from v1 (incl. URL typo fix)."""
    try:
        action = payload.get("object_attributes", {}).get("action")
        if action not in ("open", "update", "reopen"):
            logger.info("Ignoring MR action: %s", action)
            return None

        attrs = payload["object_attributes"]
        project = payload["project"]

        # opt-out: [no-review] in the title/description or a "no-review" label
        # (e.g. infra MRs where every push would re-review a huge diff for $$)
        marker_text = f"{attrs.get('title', '')} {attrs.get('description', '')}".lower()
        labels = {(lbl.get("title") or "").lower() for lbl in payload.get("labels", [])}
        if "[no-review]" in marker_text or "no-review" in labels:
            logger.info("Skipping MR !%s: no-review marker present", attrs.get("iid"))
            return None

        url = attrs.get("url", "")
        if "/-/mergerequests/" in url:
            url = url.replace("/-/mergerequests/", "/-/merge_requests/")

        return ReviewJob(
            ref=MergeRequestRef(instance, project["id"], project["path_with_namespace"],
                                attrs["iid"], url),
            mr_id=attrs["id"],
            source_branch=attrs["source_branch"],
            target_branch=attrs["target_branch"],
            title=attrs["title"],
            description=attrs.get("description") or "",
            author=payload["user"]["username"],
            action=action,
            last_commit=(attrs.get("last_commit") or {}).get("id"),
            # opt-in: force a full fresh review (skips incremental delta and
            # same-sha suppression) — e.g. to regenerate the tester report
            force_full="[re-review]" in marker_text or "re-review" in labels,
        )
    except KeyError as exc:
        logger.error("Missing required field in webhook payload: %s", exc)
        return None


def parse_note_webhook(payload: dict[str, Any], instance: InstanceRef) -> DialogueJob | None:
    """Comment ('Note Hook') on a merge request -> dialogue job, or None.

    Filters out system notes, non-MR comments and empty bodies. The bot's own
    notes are dropped by the caller (it knows the instance's bot username)."""
    try:
        if payload.get("object_kind") != "note":
            return None
        attrs = payload.get("object_attributes") or {}
        if attrs.get("noteable_type") != "MergeRequest" or attrs.get("system"):
            return None
        body = (attrs.get("note") or "").strip()
        if not body:
            return None
        mr = payload.get("merge_request") or {}
        if not mr.get("iid"):
            return None
        project = payload["project"]
        position = attrs.get("position") or {}
        pos = ""
        if isinstance(position, dict):
            path = position.get("new_path") or position.get("old_path")
            line = position.get("new_line") or position.get("old_line")
            if path:
                pos = f"{path}:{line}" if line else str(path)
        return DialogueJob(
            ref=MergeRequestRef(instance, project["id"], project["path_with_namespace"],
                                mr["iid"], mr.get("url", "")),
            note_id=attrs.get("id"),
            discussion_id=attrs.get("discussion_id") or "",
            note_body=body,
            note_author=(payload.get("user") or {}).get("username", ""),
            note_position=pos,
            last_commit=(mr.get("last_commit") or {}).get("id"),
        )
    except KeyError as exc:
        logger.error("Missing required field in note webhook payload: %s", exc)
        return None


MAX_DISCUSSION_SCAN = 300  # fallback when the payload carries no discussion_id


def discussion_context(mr, note_id, discussion_id: str = "") -> tuple[str, list[dict]]:
    """(discussion_id, notes-as-dicts) for the thread containing note_id.

    Uses the webhook's discussion_id when present; otherwise scans the MR's
    discussions (bounded). ("", []) when not found / API failure."""
    def _notes_of(disc) -> list[dict]:
        return (getattr(disc, "attributes", None) or {}).get("notes") or []
    try:
        if discussion_id:
            disc = mr.discussions.get(discussion_id)
            notes = _notes_of(disc)
            if notes:
                return discussion_id, notes
        for idx, disc in enumerate(mr.discussions.list(iterator=True)):
            if idx >= MAX_DISCUSSION_SCAN:
                break
            notes = _notes_of(disc)
            if any(n.get("id") == note_id for n in notes):
                return str(getattr(disc, "id", "")), notes
    except Exception as exc:  # noqa: BLE001 — dialogue is best-effort
        logger.warning("could not fetch discussion for note %s: %s", note_id, exc)
    return "", []


def render_thread(notes: list[dict], bot_username: str, max_chars: int = 12_000,
                  per_note_chars: int = 3000) -> str:
    """Discussion notes as a transcript; the bot's own notes are tagged so the
    model knows which side of the conversation it is."""
    parts = []
    for note in notes:
        if note.get("system"):
            continue
        author = ((note.get("author") or {}).get("username")) or "unknown"
        tag = " [bot — this is you]" if bot_username and author == bot_username else ""
        body = (note.get("body") or "").strip()
        if len(body) > per_note_chars:
            body = body[:per_note_chars] + " …[trimmed]"
        parts.append(f"[@{author}{tag}]:\n{body}")
    text = "\n\n---\n\n".join(parts)
    if len(text) > max_chars:  # keep the tail — the message being answered is last
        text = "…" + text[-max_chars:]
    return text


def thread_involves_bot(notes: list[dict], bot_username: str) -> bool:
    return bool(bot_username) and any(
        ((n.get("author") or {}).get("username")) == bot_username
        for n in notes if not n.get("system"))


def bot_answered_after(notes: list[dict], note_id, bot_username: str) -> bool:
    """A bot note NEWER than the triggering note already exists in the thread
    (webhook retry / restart redelivery) — do not answer the same message twice."""
    return bool(bot_username) and any(
        ((n.get("author") or {}).get("username")) == bot_username
        and (n.get("id") or 0) > (note_id or 0)
        for n in notes if not n.get("system"))


def mentions_user(text: str, username: str) -> bool:
    if not username:
        return False
    return re.search(rf"@{re.escape(username)}\b", text or "", re.IGNORECASE) is not None


async def post_discussion_reply(mr, discussion_id: str, body: str) -> None:
    """Reply inside an MR discussion thread. On a standalone (non-thread) note
    GitLab converts it into a thread — same endpoint either way."""
    def _post():
        discussion = mr.discussions.get(discussion_id)
        discussion.notes.create({"body": body})
    await asyncio.to_thread(_post)


def extract_jira_keys(job: ReviewJob) -> list[str]:
    """Cheap regex extraction from branch/title/description (triage may add more).
    Never from diff content: docs and fixtures contain example keys."""
    haystack = " ".join((job.source_branch, job.title, job.description))
    seen: list[str] = []
    for key in JIRA_KEY_RE.findall(haystack):
        if key not in seen:
            seen.append(key)
    return seen


def check_merge_conflicts(mr) -> bool:
    try:
        details = mr.manager.gitlab.http_get(
            f"/projects/{mr.project_id}/merge_requests/{mr.iid}")
        merge_status = details.get("merge_status", "")
        has_conflicts = (
            merge_status in ("cannot_be_merged", "cannot_be_merged_recheck")
            or details.get("has_conflicts", False)
            or details.get("blocking_discussions_resolved", True) is False
        )
        logger.debug("MR !%s merge_status=%s has_conflicts=%s",
                     mr.iid, merge_status, has_conflicts)
        return has_conflicts
    except Exception as exc:  # noqa: BLE001 — assume no conflicts if unknown
        logger.warning("Could not check merge conflicts for MR !%s: %s", mr.iid, exc)
        return False


def extract_review_content(project, mr, changes: ChangeSet,
                           skip: set[str] | None = None) -> str:
    """Diffs + current file contents (first 200 lines) — same format as v1.

    `skip` holds paths triage judged not worth reading (assets, generated
    output); they are listed at the end instead of being dumped."""
    review_parts: list[str] = []
    skipped: list[str] = []
    file_count = 0

    for change in changes.files:
        file_path = change.path or "unknown"
        diff = change.diff
        collapsed = not diff and change.collapsed
        if not diff and not collapsed:
            continue
        if skip and file_path in skip:
            skipped.append(f"{change.status}: {file_path}")
            continue

        file_count += 1
        review_parts.append(f"\n{'=' * 80}\nFILE #{file_count}: {file_path}\n{'=' * 80}")

        if collapsed:
            # GitLab withheld the diff (per-file size limit) — fall back to the
            # current file content so the reviewer still sees the code at all
            review_parts.append(
                "\n[DIFF UNAVAILABLE — GitLab collapsed it (file too large); "
                "current content below]\n")
            if change.deleted_file:
                review_parts.append("[FILE DELETED]\n")
                continue
            try:
                file_obj = project.files.get(file_path, ref=mr.source_branch)
                content = file_obj.decode().decode("utf-8", errors="replace")
                lines = content.split("\n")
                if len(lines) > 1000:
                    review_parts.append(
                        f"\n--- CURRENT FILE CONTENT (first 1000 lines of "
                        f"{len(lines)} total) ---\n")
                    review_parts.append("\n".join(lines[:1000]))
                    review_parts.append("\n... [truncated] ...\n")
                else:
                    review_parts.append("\n--- CURRENT FILE CONTENT ---\n")
                    review_parts.append(content)
            except Exception as exc:  # noqa: BLE001 — context fetch is best-effort
                logger.debug("Could not fetch file content for %s: %s", file_path, exc)
                review_parts.append(f"[Unable to fetch content: {exc}]\n")
            continue

        if change.deleted_file:
            review_parts.append("\n[FILE DELETED]\n\n--- DIFF ---\n")
            review_parts.append(diff)
            continue

        if change.new_file:
            review_parts.append("\n[NEW FILE]\n\n--- DIFF ---\n")
            review_parts.append(diff)
            continue

        try:
            file_obj = project.files.get(file_path, ref=mr.source_branch)
            content = file_obj.decode().decode("utf-8", errors="replace")
            lines = content.split("\n")
            if len(lines) > 200:
                review_parts.append(
                    f"\n--- CURRENT FILE CONTENT (first 200 lines of {len(lines)} total) ---\n")
                review_parts.append("\n".join(lines[:200]))
                review_parts.append("\n... [truncated] ...\n")
            else:
                review_parts.append("\n--- CURRENT FILE CONTENT ---\n")
                review_parts.append(content)
        except Exception as exc:  # noqa: BLE001 — context fetch is best-effort
            logger.debug("Could not fetch file content for %s: %s", file_path, exc)
            review_parts.append(f"\n--- CURRENT FILE CONTENT ---\n[Unable to fetch: {exc}]\n")

        review_parts.append("\n--- DIFF ---\n")
        review_parts.append(diff)

    if skipped:
        review_parts.append(summarize_skipped(skipped))
    return "\n".join(review_parts)


def fetch_review_guidelines(project, ref: str) -> str:
    """Per-project reviewer config: .ai-review.md at the MR target branch root.
    Teams write what to focus on / what to skip (CodeRabbit-style). Best-effort."""
    try:
        file_obj = project.files.get(".ai-review.md", ref=ref)
        text = file_obj.decode().decode("utf-8", errors="replace").strip()
        return text[:4000]  # cap: it shares the system prompt budget
    except Exception:  # noqa: BLE001 — absent file is the normal case
        return ""


def fetch_delta_changes(project, prev_sha: str, head_sha: str) -> ChangeSet | None:
    """Changes-shaped dict with only the diffs between two SHAs (incremental
    re-review). None = can't compare (force-push, GC'd sha) -> full review."""
    try:
        comp = project.repository_compare(prev_sha, head_sha)
        diffs = comp.get("diffs") if isinstance(comp, dict) else getattr(comp, "diffs", None)
        if not diffs:
            return None
        return to_changeset(diffs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("compare %s..%s failed (%s) — falling back to full review",
                       prev_sha[:8], head_sha[:8], exc)
        return None


def extract_diff_only(changes: ChangeSet, max_chars: int | None = None,
                      skip: set[str] | None = None) -> str:
    """Compact diff for the triage stage (no file contents).

    `max_chars` caps the output on a whole-file boundary and says what was
    dropped — a partial review beats refusing an oversized MR outright."""
    parts: list[str] = []
    skipped: list[str] = []
    used = omitted = 0
    for change in changes.files:
        file_path = change.path or "unknown"
        diff = change.diff
        if not change.readable:
            continue
        if skip and file_path in skip:
            skipped.append(f"{change.status}: {file_path}")
            continue
        block = (f"\n--- {file_path} ---\n{diff}" if diff else
                 f"\n--- {file_path} ---\n[diff unavailable: file too large]")
        if max_chars is not None and used + len(block) > max_chars:
            omitted += 1
            continue
        parts.append(block)
        used += len(block)
    if omitted:
        parts.append(f"\n[{omitted} more changed file(s) omitted — this MR exceeds "
                     f"the review input budget; only the files above were reviewed]")
    if skipped:
        parts.append(summarize_skipped(skipped))
    return "\n".join(parts)


def real_mr_author(mr) -> str:
    """Username of the actual MR author from the live MR object.

    The webhook's `user` field is the EVENT ACTOR (whoever pushed/edited/
    reopened), not the author — a title edit by the owner must not relabel
    someone else's MR."""
    author = getattr(mr, "author", None) or {}
    if isinstance(author, dict):
        return author.get("username") or ""
    return getattr(author, "username", "") or ""


def fetch_mr_comments(mr, bot_username: str = "", max_chars: int = 6000) -> str:
    """Human discussion on the MR (context for review/investigation).

    Skips system notes (pushes, label changes) and the bot's own notes (our
    previous reviews/reports — the incremental-review note covers those).
    Best-effort: any API failure returns an empty string."""
    try:
        notes = mr.notes.list(per_page=100, order_by="created_at", sort="asc",
                              get_all=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not fetch MR notes: %s", exc)
        return ""
    parts: list[str] = []
    for note in notes:
        if getattr(note, "system", False):
            continue
        author = getattr(note, "author", None) or {}
        username = (author.get("username", "") if isinstance(author, dict)
                    else getattr(author, "username", "")) or "unknown"
        if bot_username and username == bot_username:
            continue
        body = (getattr(note, "body", "") or "").strip()
        if body:
            parts.append(f"[{username}]: {body}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:  # keep the tail — latest replies matter most
        text = "…" + text[-max_chars:]
    return text


def mr_header(title: str, author: str, source_branch: str, target_branch: str) -> str:
    return (
        f"Merge Request: {title}\n"
        f"Author: {author}\n"
        f"Source Branch: {source_branch}\n"
        f"Target Branch: {target_branch}"
    )


def format_review_comment(review_text: str) -> str:
    headers = {"en": "## 🤖 Automated Code Review",
               "ru": "## 🤖 Автоматический обзор кода"}
    footers = {
        "en": "*This review was generated automatically by AI. "
              "Please review the feedback and address any issues before merging.*",
        "ru": "*Этот обзор был создан автоматически с помощью ИИ. "
              "Пожалуйста, изучите отзывы и устраните все проблемы перед слиянием.*",
    }
    lang = settings.pipeline.language
    return (f"{headers.get(lang, headers['en'])}\n\n{review_text.strip()}\n\n---\n"
            f"{footers.get(lang, footers['en'])}\n")


async def post_note(mr, body: str) -> None:
    await asyncio.to_thread(mr.notes.create, {"body": body})


async def upload_tester_report(project, filename: str, content: str) -> str | None:
    """Upload an .md to the project and return the markdown link snippet, or None."""
    try:
        upload = await asyncio.to_thread(
            project.upload, filename, content.encode("utf-8"))
        return upload.get("markdown") or upload.get("url")
    except Exception as exc:  # noqa: BLE001 — delivery degradation, not fatal
        logger.error("Tester report upload failed: %s", exc)
        return None
