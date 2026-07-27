"""
Tests for src/agents/rich_output.py (docs/decisions/0009-reporter-design.md).

Skipped entirely when `rich` isn't installed (RICH_AVAILABLE is False) —
matching the module's own optional-dependency design: this project's test
suite must pass in an environment WITHOUT the `rich` extra installed just
as much as one with it, since `rich` is never a hard dependency. The
fallback path itself (RICH_AVAILABLE being False, cli.py using
orchestrator.py's plain-text functions instead) was verified directly in
exactly that environment before `rich` was installed as an optional extra
for this session — not merely guarded in theory.
"""

from datetime import datetime, timezone
from io import StringIO

import pytest

import agents.rich_output as rich_output
from agents.investigator import EvidenceEntry, InvestigatorFinding, RootCauseBreakdownEntry
from agents.orchestrator import Incident, IncidentCost
from agents.remediator import RemediatorResult
from agents.rich_output import RICH_AVAILABLE
from agents.scribe import ScribeEntityResult, ScribeResult
from agents.sentinel import METHOD, Segment, SentinelFinding

pytestmark = pytest.mark.skipif(not RICH_AVAILABLE, reason="rich is an optional extra (pip install -e '.[rich]') and isn't installed")


def _make_sentinel_finding(flagged=True):
    return SentinelFinding(
        segment=Segment("UnitedHealthcare", "diabetes"), segment_claim_count=1806, segment_denial_count=375,
        segment_denial_rate=0.208, baseline_denial_rate=0.037, z_score=35.53, threshold=3.5,
        method=METHOD, flagged=flagged, summary="fabricated",
    )


def _make_investigator_finding():
    return InvestigatorFinding(
        primary_root_cause="introduced_at:claims",
        root_cause_breakdown=[RootCauseBreakdownEntry(classification="introduced_at:claims", claim_count=325, pct=86.7, note="test")],
        affected_branch=["claims", "raw_patients"], datasets_checked_and_clean=["mart_billing", "staging_patients"],
        lineage_path_walked=["get_lineage(...)"], evidence=[EvidenceEntry(step="1", tool="t", query_or_call="q", result_summary="r")],
        root_cause_summary="fabricated summary", confidence="high", backend_used="claude_code", turns_used=5,
    )


def _make_incident(flagged=True, with_investigator=True, with_scribe=False, with_remediator=False):
    return Incident(
        incident_id="INC-20260101T000000Z-unitedhealthcare-diabetes", created_at=datetime.now(timezone.utc).isoformat(),
        status="investigated" if with_investigator else "no_anomaly",
        pipeline_stages_run=["sentinel", "investigator"] if with_investigator else ["sentinel"],
        sentinel=_make_sentinel_finding(flagged=flagged),
        investigator=_make_investigator_finding() if with_investigator else None,
        cost=IncidentCost(sentinel_llm_calls=0, investigator_turns_or_calls=5, investigator_cost_usd=0.5, wall_clock_seconds=10.0),
        scribe=ScribeResult(
            incident_id="INC-1",
            entities=[ScribeEntityResult(entity_name="claims", entity_urn="urn:li:dataset:x", tag_applied=True, doc_note_added=True)],
        ) if with_scribe else None,
        remediator=RemediatorResult(
            incident_id="INC-1", status="success", pr_url="https://github.com/x/y/pull/1", owner="claims_ops_team",
        ) if with_remediator else None,
    )


def _capture(fn, *args, **kwargs) -> str:
    """Redirects rich_output's Console to an in-memory buffer so assertions
    can check real rendered content without polluting test output."""
    buf = StringIO()
    original = rich_output._console
    rich_output._console = lambda: __import__("rich.console", fromlist=["Console"]).Console(file=buf, force_terminal=False, width=120)
    try:
        fn(*args, **kwargs)
    finally:
        rich_output._console = original
    return buf.getvalue()


class TestPrintIncidentSummaryRich:
    def test_includes_segment_and_z_score(self):
        incident = _make_incident()
        out = _capture(rich_output.print_incident_summary_rich, incident)
        assert "UnitedHealthcare" in out
        assert "diabetes" in out
        assert "35.53" in out

    def test_flagged_shows_flagged_marker(self):
        incident = _make_incident(flagged=True)
        out = _capture(rich_output.print_incident_summary_rich, incident)
        assert "FLAGGED" in out

    def test_not_flagged_shows_not_flagged(self):
        incident = _make_incident(flagged=False)
        out = _capture(rich_output.print_incident_summary_rich, incident)
        assert "not flagged" in out

    def test_investigator_section_shows_implicated_and_cleared(self):
        incident = _make_incident()
        out = _capture(rich_output.print_incident_summary_rich, incident)
        assert "claims" in out
        assert "raw_patients" in out
        assert "mart_billing" in out
        assert "staging_patients" in out

    def test_no_investigator_omits_that_section(self):
        incident = _make_incident(with_investigator=False)
        out = _capture(rich_output.print_incident_summary_rich, incident)
        assert "Investigator" not in out

    def test_scribe_section_shown_when_present(self):
        incident = _make_incident(with_scribe=True)
        out = _capture(rich_output.print_incident_summary_rich, incident)
        assert "Scribe" in out
        assert "claims" in out

    def test_remediator_pr_url_shown(self):
        incident = _make_incident(with_remediator=True)
        out = _capture(rich_output.print_incident_summary_rich, incident)
        assert "https://github.com/x/y/pull/1" in out

    def test_report_line_shown_when_report_stage_ran(self):
        incident = _make_incident()
        incident.pipeline_stages_run = incident.pipeline_stages_run + ["report"]
        out = _capture(rich_output.print_incident_summary_rich, incident)
        assert "Report" in out
        assert incident.incident_id in out

    def test_written_path_shown_when_given(self):
        incident = _make_incident()
        out = _capture(rich_output.print_incident_summary_rich, incident, written_path="/tmp/fake/incident.json")
        assert "/tmp/fake/incident.json" in out


class TestPrintDryRunSummaryRich:
    def test_no_flagged_segments_says_no_anomaly(self):
        incidents = [_make_incident(flagged=False, with_investigator=False)]
        out = _capture(rich_output.print_dry_run_summary_rich, incidents, forced_segment=None)
        assert "No anomaly detected" in out

    def test_flagged_segment_appears_in_table(self):
        incidents = [_make_incident(flagged=True, with_investigator=False)]
        out = _capture(rich_output.print_dry_run_summary_rich, incidents, forced_segment=None)
        assert "UnitedHealthcare" in out
        assert "diabetes" in out
        assert "35.53" in out

    def test_forced_segment_noted(self):
        incidents = [_make_incident(flagged=False, with_investigator=False)]
        forced = Segment("UnitedHealthcare", "diabetes")
        out = _capture(rich_output.print_dry_run_summary_rich, incidents, forced_segment=forced)
        assert "forced" in out.lower()
