"""Input-size budget ladder: big MRs degrade, they are never refused.

prod !779 (655 files / 235k tokens) once got "MR too large to analyze". The
review now steps down full context -> diffs only -> as many whole file diffs
as fit, each variant saying what was dropped; the investigator gets the
largest context that fits, checked before paying for a repo clone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

# extract_review_content adds up to 200 lines of current content per file;
# ~2000 tokens each is the estimate used to decide whether fetching is worth it
FILE_CONTEXT_TOKENS_EST = 2000
# share of the input budget (in chars) the trimmed review diff may use: the
# rest is room for the system prompt, guidelines and header
REVIEW_TRIM_SHARE = 0.8
INVESTIGATOR_TRIM_SHARE = 0.6

FILE_CONTEXT_OMITTED_NOTE = (
    "\n\n[current file contents omitted — this MR is too large "
    "to include them; the diffs above are complete]")
DIFF_ONLY_HEADER_NOTE = "\n(file context omitted — MR too large; diffs only)"
TRIMMED_HEADER_NOTE = ("\n(file context omitted and the diff was truncated — this MR "
                       "exceeds the review input budget)")


def file_context_fits(diff_tokens: int, readable_files: int, max_input_tokens: int) -> bool:
    """Fetching current file contents costs ONE GitLab API call per file (~30s
    for 258 files on !779) — skip it when the result can't fit anyway."""
    return diff_tokens + readable_files * FILE_CONTEXT_TOKENS_EST <= max_input_tokens


def trim_budget_chars(max_input_tokens: int, chars_per_token: float, share: float) -> int:
    return int(max_input_tokens * chars_per_token * share)


def review_input_ladder(review_content: str, diff_only: str,
                        trimmed: Callable[[], str] | None) -> Iterator[tuple[str, str]]:
    """(header note, body) variants, largest first. Each is built only when the
    caller asks for the next one, i.e. after the previous proved too large."""
    yield "", review_content
    if not diff_only:
        return
    yield DIFF_ONLY_HEADER_NOTE, diff_only
    if trimmed is None:
        return
    yield TRIMMED_HEADER_NOTE, trimmed()
