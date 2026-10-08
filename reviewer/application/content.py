"""What the models read: review input assembled from domain models.

Pure text formatting (no SDK objects) plus two helpers that read files through
the VcsPort. Formats are unchanged from v1 — the prompts and tests depend on
the exact markers ("DIFF UNAVAILABLE", "SKIPPED — ...").
"""

from __future__ import annotations

import logging
import re

from ..domain.models import ChangeSet, MergeRequestRef, Note, ReviewJob
from .ports import VcsPort

logger = logging.getLogger(__name__)

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")

MAX_LISTED_SKIPPED_FILES = 40
MAX_MANIFEST_FILES = 600
GUIDELINES_FILE = ".ai-review.md"
GUIDELINES_MAX_CHARS = 4000  # shares the system prompt budget
CONTEXT_LINES = 200  # current-file context shown next to a diff
COLLAPSED_FALLBACK_LINES = 1000  # current content shown instead of a withheld diff


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


def _head_of(content: str, limit: int, label: str) -> list[str]:
    lines = content.split("\n")
    if len(lines) > limit:
        return [f"\n--- {label} (first {limit} lines of {len(lines)} total) ---\n",
                "\n".join(lines[:limit]), "\n... [truncated] ...\n"]
    return [f"\n--- {label} ---\n", content]


async def assemble_review_content(vcs: VcsPort, ref: MergeRequestRef, changes: ChangeSet,
                                  git_ref: str, skip: set[str] | None = None) -> str:
    """Diffs + current file contents (first 200 lines) — same format as v1.

    One file read per changed file. `skip` holds paths triage judged not worth
    reading (assets, generated output); they are listed at the end instead.
    GitLab collapses large per-file diffs to empty strings: those fall back to
    the current content with a marker, never silently vanish (MR !18)."""
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
            # the server withheld the diff (per-file size limit) — fall back to
            # the current file content so the reviewer still sees the code at all
            review_parts.append(
                "\n[DIFF UNAVAILABLE — GitLab collapsed it (file too large); "
                "current content below]\n")
            if change.deleted_file:
                review_parts.append("[FILE DELETED]\n")
                continue
            try:
                content = await vcs.read_file(ref, file_path, git_ref)
                review_parts += _head_of(content, COLLAPSED_FALLBACK_LINES,
                                         "CURRENT FILE CONTENT")
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
            content = await vcs.read_file(ref, file_path, git_ref)
            review_parts += _head_of(content, CONTEXT_LINES, "CURRENT FILE CONTENT")
        except Exception as exc:  # noqa: BLE001 — context fetch is best-effort
            logger.debug("Could not fetch file content for %s: %s", file_path, exc)
            review_parts.append(f"\n--- CURRENT FILE CONTENT ---\n[Unable to fetch: {exc}]\n")

        review_parts.append("\n--- DIFF ---\n")
        review_parts.append(diff)

    if skipped:
        review_parts.append(summarize_skipped(skipped))
    return "\n".join(review_parts)


async def read_guidelines(vcs: VcsPort, ref: MergeRequestRef, git_ref: str) -> str:
    """Per-project reviewer config: .ai-review.md at the MR target branch root.
    Teams write what to focus on / what to skip (CodeRabbit-style). Best-effort."""
    try:
        text = await vcs.read_file(ref, GUIDELINES_FILE, git_ref)
    except Exception:  # noqa: BLE001 — absent file is the normal case
        return ""
    return text.strip()[:GUIDELINES_MAX_CHARS]


def format_comments(notes: list[Note], bot_username: str = "", max_chars: int = 6000) -> str:
    """Human discussion on the MR (context for review/investigation).

    Skips system notes (pushes, label changes) and the bot's own notes (our
    previous reviews/reports — the incremental-review note covers those)."""
    parts = [f"[{note.author or 'unknown'}]: {note.body.strip()}"
             for note in notes
             if not note.system and not (bot_username and note.author == bot_username)
             and note.body.strip()]
    text = "\n\n".join(parts)
    if len(text) > max_chars:  # keep the tail — latest replies matter most
        text = "…" + text[-max_chars:]
    return text


def render_thread(notes: tuple[Note, ...] | list[Note], bot_username: str,
                  max_chars: int = 12_000, per_note_chars: int = 3000) -> str:
    """Discussion notes as a transcript; the bot's own notes are tagged so the
    model knows which side of the conversation it is."""
    parts = []
    for note in notes:
        if note.system:
            continue
        author = note.author or "unknown"
        tag = " [bot — this is you]" if bot_username and author == bot_username else ""
        body = note.body.strip()
        if len(body) > per_note_chars:
            body = body[:per_note_chars] + " …[trimmed]"
        parts.append(f"[@{author}{tag}]:\n{body}")
    text = "\n\n---\n\n".join(parts)
    if len(text) > max_chars:  # keep the tail — the message being answered is last
        text = "…" + text[-max_chars:]
    return text


def thread_involves_bot(notes: tuple[Note, ...] | list[Note], bot_username: str) -> bool:
    return bool(bot_username) and any(
        n.author == bot_username for n in notes if not n.system)


def bot_answered_after(notes: tuple[Note, ...] | list[Note], note_id: int | None,
                       bot_username: str) -> bool:
    """A bot note NEWER than the triggering note already exists in the thread
    (webhook retry / restart redelivery) — do not answer the same message twice."""
    return bool(bot_username) and any(
        n.author == bot_username and n.id > (note_id or 0)
        for n in notes if not n.system)


def mentions_user(text: str, username: str) -> bool:
    if not username:
        return False
    return re.search(rf"@{re.escape(username)}\b", text or "", re.IGNORECASE) is not None


def extract_jira_keys(job: ReviewJob) -> list[str]:
    """Cheap regex extraction from branch/title/description (triage may add more).
    Never from diff content: docs and fixtures contain example keys."""
    haystack = " ".join((job.source_branch, job.title, job.description))
    seen: list[str] = []
    for key in JIRA_KEY_RE.findall(haystack):
        if key not in seen:
            seen.append(key)
    return seen


def mr_header(title: str, author: str, source_branch: str, target_branch: str) -> str:
    return (
        f"Merge Request: {title}\n"
        f"Author: {author}\n"
        f"Source Branch: {source_branch}\n"
        f"Target Branch: {target_branch}"
    )


def format_review_comment(review_text: str, lang: str = "en") -> str:
    headers = {"en": "## 🤖 Automated Code Review",
               "ru": "## 🤖 Автоматический обзор кода"}
    footers = {
        "en": "*This review was generated automatically by AI. "
              "Please review the feedback and address any issues before merging.*",
        "ru": "*Этот обзор был создан автоматически с помощью ИИ. "
              "Пожалуйста, изучите отзывы и устраните все проблемы перед слиянием.*",
    }
    return (f"{headers.get(lang, headers['en'])}\n\n{review_text.strip()}\n\n---\n"
            f"{footers.get(lang, footers['en'])}\n")
