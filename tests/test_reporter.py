"""
Tests for src/agents/reporter.py (docs/decisions/0009-reporter-design.md).

No LLM, no network, no live calls at all -- Reporter has none of its own
(one read-only sqlite connection to the real, committed healthcare.db is
the only I/O, matching the same "reads real, live data, never assumed"
discipline as sentinel.py's own load_segment_counts()).

Two layers: pure/db-query unit tests against small synthetic sqlite dbs
(TestSeverity, TestBaselineContext, TestMemberImpact), and golden-file
determinism tests against the two REAL saved canonical incidents
(TestGoldenFileDeterminism) -- generating each report format TWICE and
diffing, with the one genuinely non-deterministic field (`generated_at`,
a real wall-clock timestamp by explicit design -- see reporter.py's own
module docstring for why it isn't omitted) named explicitly and
normalized before comparison, not blanket-stripped by a vague timestamp
regex.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agents.reporter as reporter
from agents.orchestrator import EXAMPLES_DIR, load_incident
from agents.reporter import (
    PIPELINE_TOPOLOGY,
    BaselineContext,
    load_baseline_context,
    load_member_impact,
    generate_audit_report_html,
    generate_audit_report_md,
    severity_for,
    write_audit_reports,
)

REPO_ROOT = Path(__file__).parent.parent
REAL_INCIDENT_IDS = [
    "INC-20260726T023526Z-unitedhealthcare-diabetes",
    "INC-20260724T234736Z-cigna-obesity",
]
REAL_DB_PATH = REPO_ROOT / "src" / "datahub" / "healthcare.db"


# ===========================================================================
# 1. Pure functions.
# ===========================================================================


class TestSeverity:
    def test_critical_at_20(self):
        assert severity_for(20.0) == "Critical"
        assert severity_for(35.53) == "Critical"

    def test_high_between_10_and_20(self):
        assert severity_for(10.0) == "High"
        assert severity_for(15.0) == "High"

    def test_moderate_below_10(self):
        assert severity_for(3.6) == "Moderate"
        assert severity_for(9.99) == "Moderate"


# ===========================================================================
# 2. Live db context -- small synthetic sqlite db, same shape as
#    tests/test_sentinel.py's own synthetic-data fixtures.
# ===========================================================================


@pytest.fixture
def synthetic_db(tmp_path) -> Path:
    """3 segments: A/x (10 claims, 5 denied), B/y (20 claims, 2 denied),
    C/z (5 claims, 1 denied) -- small, hand-verifiable numbers."""
    db_path = tmp_path / "synthetic.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE claims (claim_id TEXT PRIMARY KEY, insurance_provider TEXT, medical_condition TEXT)")
    conn.execute("CREATE TABLE denials (claim_id TEXT, denial_reason_code TEXT)")

    rows = []
    for i in range(10):
        rows.append((f"A{i}", "A", "x"))
    for i in range(20):
        rows.append((f"B{i}", "B", "y"))
    for i in range(5):
        rows.append((f"C{i}", "C", "z"))
    conn.executemany("INSERT INTO claims VALUES (?, ?, ?)", rows)

    denial_rows = [("A0", "INVALID_BILLING_AMOUNT"), ("A1", "INVALID_BILLING_AMOUNT"), ("A2", "INVALID_BILLING_AMOUNT"),
                    ("A3", "RANDOM_AUDIT"), ("A4", "HIGH_RISK_SCORE"),
                    ("B0", "RANDOM_AUDIT"), ("B1", "HIGH_RISK_SCORE"),
                    ("C0", "INVALID_BILLING_AMOUNT")]
    conn.executemany("INSERT INTO denials VALUES (?, ?)", denial_rows)
    conn.commit()
    conn.close()
    return db_path


class TestLoadBaselineContext:
    def test_returns_correct_segment_and_rest_counts(self, synthetic_db):
        from agents.sentinel import Segment
        conn = sqlite3.connect(str(synthetic_db))
        try:
            ctx = load_baseline_context(conn, Segment("A", "x"))
        finally:
            conn.close()
        assert ctx.segment_claims == 10
        assert ctx.segment_denials == 5
        assert ctx.rest_claims == 25  # 20 (B) + 5 (C)
        assert ctx.rest_denials == 3  # 2 (B) + 1 (C)

    def test_unknown_segment_raises(self, synthetic_db):
        from agents.sentinel import Segment
        conn = sqlite3.connect(str(synthetic_db))
        try:
            with pytest.raises(ValueError, match="not found"):
                load_baseline_context(conn, Segment("Nonexistent", "condition"))
        finally:
            conn.close()


class TestLoadMemberImpact:
    def test_returns_counts_by_reason_code_for_one_segment_only(self, synthetic_db):
        from agents.sentinel import Segment
        conn = sqlite3.connect(str(synthetic_db))
        try:
            rows = load_member_impact(conn, Segment("A", "x"))
        finally:
            conn.close()
        by_category = {r.category: r.claim_count for r in rows}
        assert by_category == {"INVALID_BILLING_AMOUNT": 3, "RANDOM_AUDIT": 1, "HIGH_RISK_SCORE": 1}

    def test_segment_with_no_denials_returns_empty(self, synthetic_db):
        from agents.sentinel import Segment
        conn = sqlite3.connect(str(synthetic_db))
        conn.execute("INSERT INTO claims VALUES ('D0', 'D', 'clean')")
        conn.commit()
        try:
            rows = load_member_impact(conn, Segment("D", "clean"))
        finally:
            conn.close()
        assert rows == []


# ===========================================================================
# 3. Golden-file determinism tests -- against the two REAL saved incidents.
# ===========================================================================


# The one field known to vary run-to-run, named explicitly (not a blanket
# timestamp regex) -- see reporter.py's own module docstring for why it's a
# real timestamp in the content rather than omitted.
_GENERATED_AT_LINE_RE = re.compile(r"^\*\*Generated\*\*: .*$", re.MULTILINE)  # MD
_GENERATED_AT_META_RE = re.compile(r"Generated: [^<]*", re.MULTILINE)  # HTML (inside the <span class="meta"> text)


def _normalize_md(text: str) -> str:
    return _GENERATED_AT_LINE_RE.sub("**Generated**: <normalized>", text)


def _normalize_html(text: str) -> str:
    return _GENERATED_AT_META_RE.sub("Generated: <normalized>", text)


@pytest.fixture(params=REAL_INCIDENT_IDS)
def real_incident(request):
    if not REAL_DB_PATH.exists():
        pytest.skip("real healthcare.db not present in this environment")
    path = EXAMPLES_DIR / request.param / "incident.json"
    if not path.exists():
        pytest.skip(f"real saved incident {request.param} not present")
    return load_incident(path)


class TestGoldenFileDeterminism:
    def test_md_is_byte_identical_across_two_generations_once_generated_at_is_normalized(self, real_incident):
        first = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        second = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)

        # The only field expected to differ between two real calls at all.
        # Every OTHER line must be identical, checked directly (not just
        # asserted after normalization, which would also pass if some
        # OTHER field had silently started varying too).
        first_lines = first.splitlines()
        second_lines = second.splitlines()
        differing = [i for i, (a, b) in enumerate(zip(first_lines, second_lines)) if a != b]
        assert all(first_lines[i].startswith("**Generated**:") for i in differing)

        assert _normalize_md(first) == _normalize_md(second)

    def test_html_is_byte_identical_across_two_generations_once_generated_at_is_normalized(self, real_incident):
        first = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)
        second = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)

        assert _normalize_html(first) == _normalize_html(second)

    def test_md_and_html_both_have_no_leftover_template_placeholders(self, real_incident):
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        html_out = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)
        # string.Template.substitute() raises on a missing key rather than
        # silently leaving `$name` in the output -- but a stray literal `$`
        # in template prose (accidentally unescaped) would slip through
        # silently, so check for it directly rather than trust that alone.
        assert "$" not in md
        assert "$" not in html_out


class TestGoldenFileContentSanity:
    """Not byte-comparison -- confirms each report actually contains the
    real, correct numbers for its own incident (would catch a determinism
    fix that's merely "stable" but wrong)."""

    def test_md_contains_real_segment_and_z_score(self, real_incident):
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        s = real_incident.sentinel
        assert s.segment.insurance_provider in md
        assert s.segment.medical_condition in md
        assert f"{s.z_score:.2f}" in md

    def test_md_recomputable_counts_sum_to_real_totals(self, real_incident):
        """The whole point of the "recomputable" design: segment + baseline
        counts shown must actually sum to the real database totals."""
        conn = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            ctx = load_baseline_context(conn, real_incident.sentinel.segment)
        finally:
            conn.close()
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        assert f"segment claims = {ctx.segment_claims}" in md
        assert f"segment denials = {ctx.segment_denials}" in md
        assert f"baseline claims = {ctx.rest_claims}" in md
        assert f"baseline denials = {ctx.rest_denials}" in md

    def test_html_lineage_diagram_colors_match_affected_and_cleared(self, real_incident):
        html_out = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)
        finding = real_incident.investigator
        for node in PIPELINE_TOPOLOGY:
            if node in finding.affected_branch:
                assert f'class="lineage-node node-implicated">{node}<' in html_out
            elif node in finding.datasets_checked_and_clean:
                assert f'class="lineage-node node-cleared">{node}<' in html_out

    def test_html_root_cause_summary_appears(self, real_incident):
        html_out = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)
        import html as html_module
        assert html_module.escape(real_incident.investigator.root_cause_summary) in html_out

    def test_md_includes_pr_url_when_remediator_succeeded(self, real_incident):
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        if real_incident.remediator is not None and real_incident.remediator.pr_url:
            assert real_incident.remediator.pr_url in md

    def test_md_includes_owner_when_known(self, real_incident):
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        if real_incident.remediator is not None and real_incident.remediator.owner:
            assert real_incident.remediator.owner in md


# ===========================================================================
# 4. write_audit_reports() -- orchestration.
# ===========================================================================


class TestWriteAuditReports:
    def test_writes_both_files_to_the_report_subdirectory(self, tmp_path):
        if not REAL_DB_PATH.exists():
            pytest.skip("real healthcare.db not present in this environment")
        path = EXAMPLES_DIR / REAL_INCIDENT_IDS[0] / "incident.json"
        if not path.exists():
            pytest.skip("real saved incident not present")
        incident = load_incident(path)

        md_path, html_path = write_audit_reports(incident, examples_dir=tmp_path, healthcare_db_path=REAL_DB_PATH)

        assert md_path == tmp_path / incident.incident_id / "report" / "audit_report.md"
        assert html_path == tmp_path / incident.incident_id / "report" / "audit_report.html"
        assert md_path.exists()
        assert html_path.exists()
        assert md_path.read_text().startswith("# Audit Report")
        assert "<!DOCTYPE html>" in html_path.read_text()
