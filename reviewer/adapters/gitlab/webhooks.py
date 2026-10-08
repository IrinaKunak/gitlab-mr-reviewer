"""GitLab webhook payloads -> jobs. Contract preserved from v1 (incl. the
merge-request URL typo fix and the [no-review] / [re-review] markers)."""

from __future__ import annotations

import logging
from typing import Any

from ...domain.models import DialogueJob, InstanceRef, MergeRequestRef, ReviewJob

logger = logging.getLogger(__name__)


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
