"""Small helpers shared by the use cases."""

from __future__ import annotations

import logging
import uuid

from .ports import VcsError, VcsPort

logger = logging.getLogger(__name__)


def new_job_id() -> str:
    """Short id that ties a queued job's log lines, alerts and MR error note."""
    return uuid.uuid4().hex[:8]


async def bot_username(vcs: VcsPort) -> str:
    """The bot's login on this instance: learned by the startup check; if
    that failed (GitLab down at boot), retried here — "" when still unknown."""
    if vcs.bot_username:
        return vcs.bot_username
    try:
        return await vcs.connect()
    except VcsError as exc:
        logger.warning("bot username unknown (%s) — own notes not filtered", exc)
        return ""
