"""Prompt templates (stage 20): the system prompts assembled from
reviewer/prompts/templates/*.md are byte-identical to the inline strings they
replaced (tests/snapshots/prompts.json) — a changed byte would move the
prompt-cache prefix. Plus the PROMPTS_DIR override."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewer import prompts
from reviewer.domain.models import TriageResult
from reviewer.prompts import TEMPLATE_NAMES, Prompts
from tests.factories import make_services, make_settings, review_job

SNAPSHOT = json.loads((Path(__file__).parent / "snapshots" / "prompts.json")
                      .read_text(encoding="utf-8"))


def test_templates_are_byte_identical():
    builtin = Prompts()
    for name in TEMPLATE_NAMES:
        assert getattr(builtin, name) == SNAPSHOT[name], name
        assert getattr(prompts, name) == SNAPSHOT[name], name  # module access
    assert builtin.REVIEW_TOOLS_NOTE.format(max_calls=8) == SNAPSHOT["REVIEW_TOOLS_NOTE.format"]
    assert (builtin.render("INCREMENTAL_REVIEW_NOTE", prev_sha="abcd1234")
            == SNAPSHOT["INCREMENTAL_REVIEW_NOTE.format"])
    assert (builtin.INVESTIGATOR_SYSTEM.format(max_iterations=30)
            == SNAPSHOT["INVESTIGATOR_SYSTEM.format"])
    assert builtin.BRIDGE_QUESTION_HINT.format(key="<KEY>") == SNAPSHOT["BRIDGE_QUESTION_HINT.format"]


def test_user_prompt_builders_unchanged():
    job = review_job(title="T", description="D" * 2100, source_branch="f", target_branch="m")
    assert prompts.TRIAGE_SCHEMA == SNAPSHOT["TRIAGE_SCHEMA"]
    assert prompts.guidelines_section("  Focus on SQL.  \n") == SNAPSHOT["guidelines_section"]
    assert prompts.triage_user_prompt(job, "diff", "manifest") == SNAPSHOT["triage_user_prompt"]
    triage = TriageResult(jira_keys=("A-1",), summary="s", risk_areas=("x", "y"))
    assert (prompts.investigator_user_prompt(job, "content", triage, "review")
            == SNAPSHOT["investigator_user_prompt"])
    assert prompts.review_user_prompt("H", "C") == SNAPSHOT["review_user_prompt"]
    assert (prompts.dialogue_user_prompt("H", "thread", "dev", "a.py:3", "diff")
            == SNAPSHOT["dialogue_user_prompt"])
    assert prompts.translate_user_prompt("x") == SNAPSHOT["translate_user_prompt"]


def test_prompts_dir_overrides_single_templates(tmp_path, caplog):
    (tmp_path / "review_system.md").write_text("Custom reviewer.\n", encoding="utf-8")
    (tmp_path / "typo_system.md").write_text("ignored", encoding="utf-8")
    custom = Prompts(tmp_path)
    assert custom.REVIEW_SYSTEM == "Custom reviewer."  # one trailing newline dropped
    assert custom.TRIAGE_SYSTEM == SNAPSHOT["TRIAGE_SYSTEM"]  # the rest stay built-in
    assert custom.overridden == ["REVIEW_SYSTEM"]
    assert "typo_system.md matches no prompt" in caplog.text

    with pytest.raises(SystemExit, match="PROMPTS_DIR"):
        Prompts(tmp_path / "missing")


def test_override_reaches_the_review_stage(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "review_system.md").write_text("Team prompt.", encoding="utf-8")
    cfg = make_settings(tmp_path, pipeline__prompts_dir=str(tmp_path / "prompts"))
    svc = make_services(cfg)
    assert svc.review_mr.review.templates.REVIEW_SYSTEM == "Team prompt."
    assert svc.answer_note.templates.DIALOGUE_SYSTEM == SNAPSHOT["DIALOGUE_SYSTEM"]
