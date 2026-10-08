"""GitLab adapter: VcsPort over python-gitlab, plus webhook parsing."""

from .client import GitLabVcs, to_changeset
from .webhooks import parse_merge_request_webhook, parse_note_webhook

__all__ = ["GitLabVcs", "parse_merge_request_webhook", "parse_note_webhook", "to_changeset"]
