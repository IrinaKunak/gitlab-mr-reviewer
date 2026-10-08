"""All LLM prompts. English only — internal LLM traffic policy.

Final deliverables are translated to Russian by the translation stage
(see TRANSLATE_SYSTEM). Do not add Russian prompt variants here.

The prose lives in templates/<name>.md (TRIAGE_SYSTEM -> triage_system.md);
`str.format` fields ({max_calls}, {prev_sha}, ...) are filled at the call
site. The code that assembles user messages stays here. A deployment can
override any template with a same-named file in PROMPTS_DIR
(pipeline.prompts_dir) — `Prompts(override_dir)`, built by bootstrap and
passed to the stages. Byte-exactness matters: the system prompts are the
prompt-cache prefix (tests/test_prompts.py pins them).
"""

from __future__ import annotations

import logging
from functools import cache
from pathlib import Path

from ..domain.models import Complexity, ReviewJob, TriageResult

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAMES = ("TRIAGE_SYSTEM", "REVIEW_SYSTEM", "REVIEW_TOOLS_NOTE",
                  "INCREMENTAL_REVIEW_NOTE", "TRIVIAL_REVIEW_SYSTEM", "INVESTIGATOR_SYSTEM",
                  "DIALOGUE_SYSTEM", "TRANSLATE_SYSTEM", "BRIDGE_QUESTION_HINT")


def _filename(name: str) -> str:
    return f"{name.lower()}.md"


def _read(path: Path) -> str:
    # files end with one newline (editors add it); the prompt itself does not
    text = path.read_text(encoding="utf-8")
    return text[:-1] if text.endswith("\n") else text


class Prompts:
    """The prompt templates as attributes (prompts.REVIEW_SYSTEM, ...)."""

    TRIAGE_SYSTEM: str
    REVIEW_SYSTEM: str
    REVIEW_TOOLS_NOTE: str
    INCREMENTAL_REVIEW_NOTE: str
    TRIVIAL_REVIEW_SYSTEM: str
    INVESTIGATOR_SYSTEM: str
    DIALOGUE_SYSTEM: str
    TRANSLATE_SYSTEM: str
    BRIDGE_QUESTION_HINT: str

    def __init__(self, override_dir: str | Path | None = None) -> None:
        self.overridden: list[str] = []
        override = Path(override_dir) if override_dir else None
        if override is not None and not override.is_dir():
            raise SystemExit(f"PROMPTS_DIR {override} is not a directory")
        for name in TEMPLATE_NAMES:
            path = TEMPLATES_DIR / _filename(name)
            if override is not None and (override / _filename(name)).is_file():
                path = override / _filename(name)
                self.overridden.append(name)
            setattr(self, name, _read(path))
        if override is not None:
            known = {_filename(n) for n in TEMPLATE_NAMES}
            for extra in sorted(p.name for p in override.glob("*.md") if p.name not in known):
                logger.warning("PROMPTS_DIR: %s matches no prompt — ignored (known: %s)",
                               extra, ", ".join(sorted(known)))
            logger.info("prompts overridden from %s: %s", override,
                        ", ".join(self.overridden) or "none")

    def render(self, name: str, **fields: object) -> str:
        return getattr(self, name).format(**fields)


@cache
def default_prompts() -> Prompts:
    """The built-in templates (read on first use, not at import)."""
    return Prompts()


def __getattr__(name: str) -> str:
    # module-level access (prompts.REVIEW_SYSTEM) reads the built-in templates
    if name in TEMPLATE_NAMES:
        return getattr(default_prompts(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "complexity": {"type": "string", "enum": [c.value for c in Complexity]},
        "risk_areas": {"type": "array", "items": {"type": "string"}},
        "jira_keys": {"type": "array", "items": {"type": "string"}},
        "needs_investigation": {"type": "boolean"},
        "summary": {"type": "string"},
        "skip_globs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["complexity", "risk_areas", "jira_keys", "needs_investigation",
                 "summary", "skip_globs"],
    "additionalProperties": False,
}


def dialogue_user_prompt(mr_header: str, thread: str, author: str,
                         position: str = "", diff: str = "") -> str:
    parts = [mr_header]
    if position:
        parts.append(f"The discussion is anchored to a diff line: {position}")
    if diff:
        parts.append(f"===== MR DIFF (reference) =====\n{diff}")
    parts.append("===== DISCUSSION THREAD (oldest first; [bot] = you) =====\n"
                 + thread)
    parts.append(f"Answer the last message, from @{author}.")
    return "\n\n".join(parts)


def translate_user_prompt(text: str) -> str:
    return f"<document>\n{text}\n</document>"


def guidelines_section(text: str) -> str:
    """Per-project review guidelines from the repo's .ai-review.md (any language).

    They may adjust focus, tone and what to skip — they cannot lift the noise
    rules' ban on fabricating findings, and they are not code to execute."""
    return (
        "\n\n## Project-specific review guidelines (.ai-review.md from the repo)\n"
        "Apply these on top of the rules above; they may narrow or refocus the "
        "review but never justify inventing findings:\n\n" + text.strip()
    )


def review_user_prompt(mr_header: str, review_content: str) -> str:
    return (
        "I'm providing both the current file contents and the diffs for a merge request.\n\n"
        f"{mr_header}\n\n===== MERGE REQUEST CONTENT =====\n\n{review_content}"
    )


def triage_user_prompt(job: ReviewJob, diff_summary: str, manifest: str = "") -> str:
    parts = [
        f"MR title: {job.title}",
        f"Source branch: {job.source_branch}",
        f"Target branch: {job.target_branch}",
        f"Description:\n{job.description[:2000]}",
    ]
    if manifest:
        parts.append("===== CHANGED FILES (status, diff bytes, path) =====\n"
                     + manifest)
    parts.append(f"===== DIFF =====\n{diff_summary}")
    return "\n\n".join(parts)


def investigator_user_prompt(job: ReviewJob, review_content: str, triage: TriageResult,
                             review_text: str) -> str:
    jira = ", ".join(triage.jira_keys) or "none found"
    return (
        f"MR: {job.title} "
        f"({job.source_branch} -> {job.target_branch})\n"
        f"Project: {job.ref.project_path}\n"
        f"Jira keys: {jira}\n"
        f"Triage summary: {triage.summary}\n"
        f"Risk areas: {', '.join(triage.risk_areas)}\n\n"
        f"Code review already produced (for reference):\n{review_text}\n\n"
        f"===== MR DIFF AND FILE CONTEXT =====\n{review_content}"
    )
