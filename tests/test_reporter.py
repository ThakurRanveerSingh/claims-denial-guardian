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
from agents.orchestrator import EXAMPLES_DIR, Incident, IncidentCost, load_incident
from agents.reporter import (
    PIPELINE_TOPOLOGY,
    BaselineContext,
    _actions_taken_lines,
    load_baseline_context,
    load_member_impact,
    generate_audit_report_html,
    generate_audit_report_md,
    severity_for,
    write_audit_reports,
)
from agents.drift import DriftFinding, FeatureHealthCheck
from agents.scribe import ScribeEntityResult, ScribeResult
from agents.sentinel import METHOD, Segment, SentinelFinding

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


@pytest.fixture(params=REAL_INCIDENT_IDS)
def real_incident_with_drift(request):
    """Sprint 3 WP4: a real saved incident with a fabricated (not live)
    DriftFinding attached -- exercises the Model Health Check section's
    content/leak-safety without a live MCP call in the regular suite. One
    check deliberately flagged so both "Passed"/"Flagged" wording paths
    are exercised, not just the all-pass case both real incidents
    currently have."""
    if not REAL_DB_PATH.exists():
        pytest.skip("real healthcare.db not present in this environment")
    path = EXAMPLES_DIR / request.param / "incident.json"
    if not path.exists():
        pytest.skip(f"real saved incident {request.param} not present")
    incident = load_incident(path)
    incident.drift = DriftFinding(
        check_id="drift-20260101T000000Z", model_version="toy-denial-risk-v1",
        checked_at="2026-01-01T00:00:00+00:00",
        feature_checks=[
            FeatureHealthCheck(
                feature_name="segment_denial_rate", check_type="range_invariant",
                documented_expected="[0.0, 1.0]", metric_value=0.0, status="pass",
                plain_summary="segment_denial_rate stayed within its valid range for every scored claim.",
            ),
            FeatureHealthCheck(
                feature_name="billing_zscore", check_type="cap_exceedance",
                documented_expected="|z| <= 4.0 (BILLING_ZSCORE_CAP, score_claims.py)", metric_value=0.4,
                status="pass",
                plain_summary="0.40% of scored claims have a billing_zscore beyond the model's own documented boundary.",
            ),
            FeatureHealthCheck(
                feature_name="billing_zscore", check_type="shape_vs_theoretical",
                documented_expected="PSI < 0.10 vs. theoretical standard normal", metric_value=0.38,
                status="flagged",
                plain_summary="billing_zscore's observed shape diverged from the theoretical standard normal by more than the healthy threshold.",
            ),
        ],
        overall_status="1 check(s) flagged",
    )
    return incident


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
        """The whole point of the "recomputable" design: the four raw
        counts a reader would need to hand-verify the z-score must
        actually appear in the report, and must be the real database
        totals -- checked by number, not by one fixed phrase (the exact
        wording changed in the Part C UAT cleanup that removed the
        function-name/file-path leak from this same sentence)."""
        conn = sqlite3.connect(f"file:{REAL_DB_PATH}?mode=ro", uri=True)
        try:
            ctx = load_baseline_context(conn, real_incident.sentinel.segment)
        finally:
            conn.close()
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        assert str(ctx.segment_claims) in md
        assert str(ctx.segment_denials) in md
        assert str(ctx.rest_claims) in md
        assert str(ctx.rest_denials) in md
        # And a real, independent recomputation from those four counts must
        # match the report's own stated z-score.
        p_segment = ctx.segment_denials / ctx.segment_claims
        p_rest = ctx.rest_denials / ctx.rest_claims
        p_pool = (ctx.segment_denials + ctx.rest_denials) / (ctx.segment_claims + ctx.rest_claims)
        se = (p_pool * (1 - p_pool) * (1 / ctx.segment_claims + 1 / ctx.rest_claims)) ** 0.5
        recomputed_z = (p_segment - p_rest) / se
        assert f"{recomputed_z:.2f}" in md

    def test_md_and_html_have_no_function_names_file_paths_or_raw_lineage_syntax_outside_appendix(self, real_incident):
        """Part C UAT finding: the narrative sections (What was detected /
        What the investigation established) must be readable by a
        non-technical compliance reviewer standalone -- no Python function
        names, source file paths, or raw get_lineage(...) tool-call syntax
        outside the Technical Appendix."""
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        html_out = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)

        narrative_md = md.split("## Technical Appendix")[0]
        assert "two_proportion_z_test(" not in narrative_md
        assert "src/agents/sentinel.py" not in narrative_md
        assert "get_lineage(" not in narrative_md
        assert "mcp__datahub__" not in narrative_md

        narrative_html = html_out.split('<h2>Technical Appendix</h2>')[0]
        assert "two_proportion_z_test(" not in narrative_html
        assert "src/agents/sentinel.py" not in narrative_html
        assert "get_lineage(" not in narrative_html
        assert "mcp__datahub__" not in narrative_html

        # And the raw trace must still exist SOMEWHERE -- moved, not deleted.
        assert "two_proportion_z_test(" in md
        assert "get_lineage(" in md or (real_incident.investigator and not real_incident.investigator.lineage_path_walked)

    def test_md_and_html_breakdown_table_has_no_raw_coded_tags(self, real_incident):
        """The Root cause breakdown table stays in the main body (not the
        appendix) but must show plain language, not e.g.
        "introduced_at:claims" verbatim."""
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        html_out = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)
        for finding_entry in (real_incident.investigator.root_cause_breakdown if real_incident.investigator else []):
            assert finding_entry.classification not in md
            assert finding_entry.classification not in html_out

    def test_html_lineage_diagram_colors_match_affected_and_cleared(self, real_incident):
        html_out = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)
        finding = real_incident.investigator
        for node in PIPELINE_TOPOLOGY:
            if node in finding.affected_branch:
                assert f'class="lineage-node node-implicated">{node}<' in html_out
            elif node in finding.datasets_checked_and_clean:
                assert f'class="lineage-node node-cleared">{node}<' in html_out

    def test_html_lineage_caption_states_correct_implicated_count(self, real_incident):
        """Part C UAT fix: a one-line caption stating "N of 5 pipeline
        stages implicated", computed from the SAME node data the diagram
        itself renders (not a second, independently-derived count)."""
        html_out = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)
        finding = real_incident.investigator
        implicated_count = sum(1 for node in PIPELINE_TOPOLOGY if node in finding.affected_branch)
        assert f'<p class="lineage-caption"><strong>{implicated_count} of {len(PIPELINE_TOPOLOGY)} pipeline stages implicated</strong></p>' in html_out

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
# 3a. Model Health Check section (Sprint 3 WP4) -- same leak-safety
#     discipline decision 0009 §4 already established, extended here
#     rather than re-litigated: drift.FeatureHealthCheck.documented_expected
#     carries a file reference ("score_claims.py") by design (it's meant
#     for the Technical Appendix, not the main narrative), so this is
#     checked explicitly rather than assumed safe by association with the
#     already-sanitized plain_summary strings.
# ===========================================================================


class TestModelHealthSection:
    def test_no_drift_finding_says_so_plainly(self, real_incident):
        assert real_incident.drift is None  # both real incidents predate WP4
        md = generate_audit_report_md(real_incident, healthcare_db_path=REAL_DB_PATH)
        html_out = generate_audit_report_html(real_incident, healthcare_db_path=REAL_DB_PATH)
        assert "No feature-health check has been run" in md
        assert "No feature-health check has been run" in html_out

    def test_with_drift_finding_shows_feature_names_and_verdicts(self, real_incident_with_drift):
        md = generate_audit_report_md(real_incident_with_drift, healthcare_db_path=REAL_DB_PATH)
        html_out = generate_audit_report_html(real_incident_with_drift, healthcare_db_path=REAL_DB_PATH)
        for text in (md, html_out):
            assert "segment_denial_rate" in text
            assert "billing_zscore" in text
            assert "Passed" in text
            assert "Flagged" in text

    def test_documented_expected_and_check_id_appear_only_in_appendix_not_main_body(self, real_incident_with_drift):
        """drift.py's `documented_expected` field deliberately carries a
        code reference ("BILLING_ZSCORE_CAP, score_claims.py") -- that's
        fine in the Technical Appendix, not in the compliance-facing
        Model Health Check section above it. Same split-on-appendix-marker
        method the existing leak test already uses."""
        md = generate_audit_report_md(real_incident_with_drift, healthcare_db_path=REAL_DB_PATH)
        html_out = generate_audit_report_html(real_incident_with_drift, healthcare_db_path=REAL_DB_PATH)

        narrative_md = md.split("## Technical Appendix")[0]
        narrative_html = html_out.split('<h2>Technical Appendix</h2>')[0]

        assert "score_claims.py" not in narrative_md
        assert "score_claims.py" not in narrative_html
        assert real_incident_with_drift.drift.check_id not in narrative_md
        assert real_incident_with_drift.drift.check_id not in narrative_html

        # Moved, not deleted -- the appendix must still have the full
        # technical detail.
        assert "score_claims.py" in md
        assert real_incident_with_drift.drift.check_id in md

    def test_plain_summaries_appear_verbatim_in_main_body(self, real_incident_with_drift):
        md = generate_audit_report_md(real_incident_with_drift, healthcare_db_path=REAL_DB_PATH)
        for c in real_incident_with_drift.drift.feature_checks:
            assert c.plain_summary in md


# ===========================================================================
# 3b. Actions Taken wording -- Part C UAT re-check finding: an earlier
#     version collapsed "already present" and "freshly applied/added" into
#     the SAME word, which meant the report's own claim couldn't reflect a
#     real, sometimes-mixed idempotency result (e.g. Cigna's live writeback
#     run: raw_patients/claims already had the tag from the earlier UHC
#     run, staging_patients/mart_billing did not). Caught by checking the
#     report text against the live terminal output, not by re-reading code
#     -- this test exists so that exact bug class can't silently return.
# ===========================================================================


def _make_incident_for_actions_test(scribe=None, remediator=None):
    sentinel = SentinelFinding(
        segment=Segment("TestProvider", "test-condition"), segment_claim_count=100, segment_denial_count=20,
        segment_denial_rate=0.2, baseline_denial_rate=0.05, z_score=10.0, threshold=3.5,
        method=METHOD, flagged=True, summary="fabricated",
    )
    return Incident(
        incident_id="INC-TEST", created_at=datetime.now(timezone.utc).isoformat(), status="investigated",
        pipeline_stages_run=["sentinel", "investigator"], sentinel=sentinel, investigator=None,
        cost=IncidentCost(sentinel_llm_calls=0, investigator_turns_or_calls=1, investigator_cost_usd=0.1, wall_clock_seconds=1.0),
        scribe=scribe, remediator=remediator,
    )


class TestActionsTakenWording:
    def test_distinguishes_already_present_from_freshly_applied(self):
        """The exact real scenario this was caught against: one entity
        with everything already present (shared with an earlier incident),
        one entity where everything is genuinely new."""
        scribe = ScribeResult(
            incident_id="INC-TEST",
            entities=[
                ScribeEntityResult(entity_name="claims", entity_urn="urn:li:dataset:x", tag_already_present=True, doc_note_already_present=True),
                ScribeEntityResult(entity_name="mart_billing", entity_urn="urn:li:dataset:y", tag_applied=True, doc_note_added=True),
            ],
        )
        lines = _actions_taken_lines(_make_incident_for_actions_test(scribe=scribe))
        text = "\n".join(lines)

        assert "claims: tag already present, documentation note already present" in text
        assert "mart_billing: tag applied, documentation note added" in text
        # The old, collapsed wording must not appear for the already-present entity.
        assert "claims: tag applied" not in text

    def test_mixed_tag_already_present_but_doc_note_fresh(self):
        """The other real half of the same scenario: a tag shared with an
        earlier incident (already present) while THIS incident's own
        documentation note is still genuinely new -- the two facts must be
        reported independently, not conflated into one status."""
        scribe = ScribeResult(
            incident_id="INC-TEST",
            entities=[ScribeEntityResult(entity_name="raw_patients", entity_urn="urn:li:dataset:z", tag_already_present=True, doc_note_added=True)],
        )
        lines = _actions_taken_lines(_make_incident_for_actions_test(scribe=scribe))
        text = "\n".join(lines)
        assert "raw_patients: tag already present, documentation note added" in text

    def test_not_applied_reported_honestly(self):
        scribe = ScribeResult(
            incident_id="INC-TEST",
            entities=[ScribeEntityResult(entity_name="mart_demographics", entity_urn="urn:li:dataset:w")],
        )
        lines = _actions_taken_lines(_make_incident_for_actions_test(scribe=scribe))
        text = "\n".join(lines)
        assert "mart_demographics: tag not applied, documentation note not added" in text

    def test_real_incidents_actions_taken_shows_already_present_not_reapplied(self):
        """Direct check against the real, currently-saved Cigna incident.
        Its writeback was resumed TWICE this session (once for the real
        backfill, once to prove idempotency directly) -- the file on disk
        now reflects that second, fully-idempotent run, so every entity
        should show already-present for both tag and doc note. This is
        still a real, meaningful check: it confirms the report's own text
        agrees with the idempotency that was independently verified live
        against DataHub, not just that the wording LOOKS plausible."""
        path = EXAMPLES_DIR / "INC-20260724T234736Z-cigna-obesity" / "incident.json"
        if not path.exists():
            pytest.skip("real saved incident not present")
        incident = load_incident(path)
        if incident.scribe is None:
            pytest.skip("this real incident has no scribe backfill yet")
        lines = _actions_taken_lines(incident)
        text = "\n".join(lines)
        entity_lines = [line for line in lines if line.strip().startswith(("raw_patients:", "staging_patients:", "mart_billing:", "claims:"))]
        assert entity_lines, "expected at least one entity line in Actions Taken"
        for line in entity_lines:
            assert "already present" in line, f"expected already-present on a resumed-twice incident, got: {line!r}"


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
