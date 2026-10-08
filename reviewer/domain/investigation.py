"""Investigator output handling."""

from __future__ import annotations

from .models import Investigation

TESTER_REPORT_MARKER = "## TESTER REPORT"


def split_investigation(text: str) -> tuple[str, str | None]:
    """Split investigator output into (impact analysis, optional tester report)."""
    report = None
    impact = text
    if TESTER_REPORT_MARKER in text:
        impact, rest = text.split(TESTER_REPORT_MARKER, 1)
        report = TESTER_REPORT_MARKER + "\n\n" + rest.strip()
    return impact.strip(), report


def investigation_from_text(text: str) -> Investigation:
    impact, report = split_investigation(text)
    return Investigation(full_text=text, impact=impact, tester_report=report)
