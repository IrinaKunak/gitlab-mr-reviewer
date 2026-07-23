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

logger = logging.getLogger(__name__)

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def get_gitlab_client(gitlab_config: dict) -> gitlab.Gitlab:
    session = requests.Session()
    proxies = settings.requests_proxies
    if proxies:
        session.proxies = proxies
    gl = gitlab.Gitlab(
        gitlab_config["url"], private_token=gitlab_config["token"], session=session)
    gl.auth()
    return gl


def parse_merge_request_webhook(payload: dict[str, Any]) -> dict[str, Any] | None:
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

        return {
            "project_id": project["id"],
            "project_path": project["path_with_namespace"],
            "mr_iid": attrs["iid"],
            "mr_id": attrs["id"],
            "source_branch": attrs["source_branch"],
            "target_branch": attrs["target_branch"],
            "title": attrs["title"],
            "description": attrs.get("description", ""),
            "author": payload["user"]["username"],
            "action": action,
            "url": url,
            "last_commit": attrs.get("last_commit", {}).get("id"),
        }
    except KeyError as exc:
        logger.error("Missing required field in webhook payload: %s", exc)
        return None


def extract_jira_keys(mr_data: dict) -> list[str]:
    """Cheap regex extraction from branch/title/description (triage may add more)."""
    haystack = " ".join(
        str(mr_data.get(key, "")) for key in ("source_branch", "title", "description"))
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


def extract_review_content(project, mr, changes: dict[str, Any]) -> str:
    """Diffs + current file contents (first 200 lines) — same format as v1."""
    review_parts: list[str] = []
    file_count = 0

    for change in changes.get("changes", []):
        file_path = change.get("new_path", change.get("old_path", "unknown"))
        diff = change.get("diff", "")
        collapsed = not diff and (change.get("collapsed") or change.get("too_large"))
        if not diff and not collapsed:
            continue

        file_count += 1
        review_parts.append(f"\n{'=' * 80}\nFILE #{file_count}: {file_path}\n{'=' * 80}")

        if collapsed:
            # GitLab withheld the diff (per-file size limit) — fall back to the
            # current file content so the reviewer still sees the code at all
            review_parts.append(
                "\n[DIFF UNAVAILABLE — GitLab collapsed it (file too large); "
                "current content below]\n")
            if change.get("deleted_file"):
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

        if change.get("deleted_file"):
            review_parts.append("\n[FILE DELETED]\n\n--- DIFF ---\n")
            review_parts.append(diff)
            continue

        if change.get("new_file"):
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


def fetch_delta_changes(project, prev_sha: str, head_sha: str) -> dict[str, Any] | None:
    """Changes-shaped dict with only the diffs between two SHAs (incremental
    re-review). None = can't compare (force-push, GC'd sha) -> full review."""
    try:
        comp = project.repository_compare(prev_sha, head_sha)
        diffs = comp.get("diffs") if isinstance(comp, dict) else getattr(comp, "diffs", None)
        if not diffs:
            return None
        return {"changes": diffs}
    except Exception as exc:  # noqa: BLE001
        logger.warning("compare %s..%s failed (%s) — falling back to full review",
                       prev_sha[:8], head_sha[:8], exc)
        return None


def extract_diff_only(changes: dict[str, Any]) -> str:
    """Compact diff for the triage stage (no file contents)."""
    parts = []
    for change in changes.get("changes", []):
        file_path = change.get("new_path", change.get("old_path", "unknown"))
        diff = change.get("diff", "")
        if diff:
            parts.append(f"\n--- {file_path} ---\n{diff}")
        elif change.get("collapsed") or change.get("too_large"):
            parts.append(f"\n--- {file_path} ---\n[diff unavailable: file too large]")
    return "\n".join(parts)


def mr_header(mr_data: dict) -> str:
    return (
        f"Merge Request: {mr_data['title']}\n"
        f"Author: {mr_data['author']}\n"
        f"Source Branch: {mr_data['source_branch']}\n"
        f"Target Branch: {mr_data['target_branch']}"
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
    lang = settings.review_language
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
