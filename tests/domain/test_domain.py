"""Pure domain layer: models, skip rules, budget ladder, dedupe, investigation.

No fakes, no settings, no event loop — plain values in, plain values out.
"""

from __future__ import annotations

from dataclasses import replace

from reviewer.domain import budget
from reviewer.domain.dedupe import DedupePolicy
from reviewer.domain.investigation import investigation_from_text, split_investigation
from reviewer.domain.models import ChangeSet, Complexity, FileChange, Tier, TriageResult
from reviewer.domain.skip import resolve_skip
from tests.factories import dialogue_job, mr_ref, review_job


def _changes(*paths: str) -> ChangeSet:
    return ChangeSet(tuple(FileChange(old_path=p, new_path=p, diff="+x") for p in paths))


# --- enums ---

def test_enums_are_the_plain_strings_config_and_logs_use():
    # usage.jsonl, model_overrides.json and the dashboard all key on these
    assert Tier.FAST == "fast" and {Tier.MAIN: 1}["main"] == 1
    assert [t.value for t in Tier] == ["fast", "main", "smart"]
    assert str(review_job().kind) == "review" and str(dialogue_job().kind) == "dialogue"


# --- triage ---

def test_triage_result_normalizes_model_output():
    # the fast tier's JSON is parsed leniently (fallback parser): odd shapes
    # must degrade to "normal review", never crash the pipeline
    triage = TriageResult.from_model(
        {"complexity": "complex", "needs_investigation": True, "risk_areas": ["auth"],
         "jira_keys": ["PBV-1"], "summary": "s", "skip_globs": ["*.svg", 7]},
        extra_jira_keys=("PBV-1", "ABC-2"))
    assert triage.complexity is Complexity.COMPLEX and triage.needs_investigation
    assert triage.jira_keys == ("PBV-1", "ABC-2")  # regex keys merged, no dupes
    assert triage.skip_globs == ("*.svg",)
    odd = TriageResult.from_model({"complexity": "medium", "jira_keys": "PBV-1",
                                   "risk_areas": None, "skip_globs": "nope"})
    assert odd.complexity is Complexity.NORMAL  # unknown -> normal, as the string compare did
    assert odd.jira_keys == () and odd.risk_areas == () and odd.skip_globs == ()


# --- skip ---

def test_resolve_skip_patterns_and_guards():
    # MR !779 (655 files, 235k tokens): triage returns PATTERNS, not paths —
    # listing 439 SVGs individually blew the fast tier's max_tokens
    changes = _changes("src/auth.py", "public/logo.svg", "yarn.lock", "src/pay.py")
    assert resolve_skip(changes, ["*.svg", "yarn.lock"]) == {"public/logo.svg", "yarn.lock"}
    assert resolve_skip(changes, ["public/*"]) == {"public/logo.svg"}
    assert resolve_skip(changes, ["public/"]) == {"public/logo.svg"}  # bare directory
    # guards: a catch-all or an everything-matching verdict is discarded, so a
    # bad triage can never silence the review
    assert resolve_skip(changes, ["*"]) == set()
    assert resolve_skip(changes, ["*.py", "*.svg", "*.lock"]) == set()
    assert resolve_skip(changes, []) == set()


# --- budget ladder ---

def test_file_context_fetch_skipped_when_it_cannot_fit():
    assert budget.file_context_fits(10_000, 10, 300_000)
    # !779: 258 readable files * ~2000 tok of context blows any budget
    assert not budget.file_context_fits(150_000, 258, 300_000)


def test_review_input_ladder_degrades_lazily():
    built = []

    def trimmed():
        built.append(1)
        return "subset"

    ladder = budget.review_input_ladder("full", "diff", trimmed)
    assert next(ladder) == ("", "full")
    note, body = next(ladder)
    assert body == "diff" and "diffs only" in note
    assert built == []  # the trimmed subset is only built when asked for
    note, body = next(ladder)
    assert body == "subset" and "truncated" in note and built == [1]
    # no diff to fall back to -> the full variant is the only one
    assert list(budget.review_input_ladder("full", "", trimmed)) == [("", "full")]
    assert len(list(budget.review_input_ladder("full", "diff", None))) == 2
    assert budget.trim_budget_chars(100_000, 4, budget.REVIEW_TRIM_SHARE) == 320_000


# --- dedupe ---

def test_dedupe_exact_retry_and_new_push():
    policy = DedupePolicy(ttl=600, burst_window=0)
    job = review_job(mr_iid=7, last_commit="abc")
    assert policy.admit(job, now=0) is True
    assert policy.admit(job, now=1) is False                       # webhook retry
    assert policy.admit(replace(job, last_commit="def"), now=2) is True  # new push
    assert policy.admit(job, now=700) is True                      # TTL expired


def test_dedupe_burst_collapses_multi_event_actions():
    # regression: reopening an MR after new pushes makes GitLab emit reopen +
    # update events with DIFFERENT shas ~1s apart -> two parallel reviews
    policy = DedupePolicy(ttl=600, burst_window=30)
    job = review_job(project_id=132, mr_iid=18, last_commit="aaa")
    assert policy.admit(job, 0) is True
    assert policy.admit(replace(job, last_commit="bbb"), 1) is False  # same MR, same instant
    assert policy.admit(job, 2) is False                              # exact duplicate
    assert policy.admit(replace(job, last_commit="ccc"), 40) is True  # window passed
    other_instance = replace(job, ref=mr_ref(project_id=132, mr_iid=18,
                                             instance=replace(job.ref.instance, name="b")))
    assert policy.admit(other_instance, 41) is True  # instance is part of the key

    no_burst = DedupePolicy(ttl=600, burst_window=0)
    assert no_burst.admit(job, 0) is True
    assert no_burst.admit(replace(job, last_commit="bbb"), 0) is True  # window=0 disables it


def test_dedupe_force_full_bypasses_and_notes_ignore_burst():
    policy = DedupePolicy(ttl=600, burst_window=300)
    job = review_job(mr_iid=7, last_commit="abc")
    assert policy.admit(job, 0) is True
    assert policy.admit(job, 1) is False
    # the re-review label event carries the same sha the TTL window swallows
    assert policy.admit(replace(job, force_full=True), 2) is True
    # a reply seconds after the review event is EXACTLY the dialogue case —
    # the per-MR burst window must not swallow it; retries of a note are deduped
    note = dialogue_job(mr_iid=7, last_commit="abc", note_id=900)
    assert policy.admit(note, 3) is True
    assert policy.admit(note, 4) is False
    assert policy.admit(replace(note, note_id=901), 5) is True


# --- investigation ---

def test_split_investigation():
    # regression: with TESTER_REPORT=off the whole investigation (impact analysis
    # included) was silently discarded — only the tester report is flag-gated
    impact, report = split_investigation(
        "Impact: touches auth.\n\n## TESTER REPORT\n\nVerify login.")
    assert impact == "Impact: touches auth."
    assert report == "## TESTER REPORT\n\nVerify login."

    impact2, report2 = split_investigation("Analysis only, no report section.")
    assert impact2 == "Analysis only, no report section."
    assert report2 is None

    inv = investigation_from_text("A\n## TESTER REPORT\nB")
    assert (inv.impact, inv.tester_report) == ("A", "## TESTER REPORT\n\nB")
    assert inv.full_text == "A\n## TESTER REPORT\nB"
