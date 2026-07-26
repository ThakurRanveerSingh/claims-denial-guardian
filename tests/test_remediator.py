"""
Tests for src/agents/remediator.py (docs/decisions/0008-remediator-design.md).

Same layering as tests/test_scribe.py: pure-function tests (no mocking),
mocked-MCP/mocked-git/mocked-LLM orchestration tests, and ONE
@pytest.mark.live end-to-end test (excluded by default, pytest.ini's
`addopts = -m "not live"`) that generates a real fix, validates it against
a scratch copy of the real healthcare.db, and opens a REAL PR on
denial-guardian-data-platform via `gh`.

The retry-loop tests below deliberately use the REAL
apply_and_validate_fix()/scratch_copy() from src/codegen/sql_validation.py
against small synthetic sqlite dbs (tmp_path), not a mock of that module —
the whole point of the "LLM proposes, code verifies" split is that the
verification is real, deterministic code; mocking it away would test
nothing but the retry bookkeeping.
"""

import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import agents.remediator as remediator
from agents.remediator import (
    FIX_TARGET_CLAIMS,
    FIX_TARGET_STAGING,
    _branch_name_for,
    _build_fix_prompt,
    _build_pr_body,
    _existing_pr_url,
    _extract_sql,
    _first_owner_name,
    _fix_target_for,
    _format_schema,
    _generate_and_validate,
    _incident_github_url,
    _open_pr,
    _pr_title,
    _schema_fields,
    run_remediator,
)
from agents.llm_backend import CompletionResult, LLMBackendError
from agents.investigator import EvidenceEntry, InvestigatorFinding, RootCauseBreakdownEntry
from agents.orchestrator import Incident, IncidentCost
from agents.sentinel import METHOD, Segment, SentinelFinding


# ===========================================================================
# Shared fixtures / helpers -- same shape as tests/test_scribe.py's.
# ===========================================================================


def _make_sentinel_finding(provider="UnitedHealthcare", condition="diabetes"):
    return SentinelFinding(
        segment=Segment(provider, condition), segment_claim_count=361, segment_denial_count=108,
        segment_denial_rate=0.30, baseline_denial_rate=0.05, z_score=13.7, threshold=3.5,
        method=METHOD, flagged=True, summary="fabricated",
    )


def _make_investigator_finding(root_cause="introduced_at:claims", affected_branch=("claims",)):
    return InvestigatorFinding(
        primary_root_cause=root_cause,
        root_cause_breakdown=[
            RootCauseBreakdownEntry(classification=root_cause, claim_count=325, pct=90.0, note="test"),
        ],
        affected_branch=list(affected_branch),
        datasets_checked_and_clean=["mart_billing"],
        lineage_path_walked=["claims", "mart_billing"],
        evidence=[EvidenceEntry(step="1", tool="t", query_or_call="q", result_summary="r")],
        root_cause_summary="A sign-flip bug at claims' own build introduces negative billing_amount for 90% of denials in this segment.",
        confidence="high", backend_used="claude_code", turns_used=5,
    )


def _make_incident(root_cause="introduced_at:claims", incident_id="INC-20260101T000000Z-unitedhealthcare-diabetes", investigator=True):
    return Incident(
        incident_id=incident_id, created_at="2026-01-01T00:00:00+00:00",
        status="investigated" if investigator else "no_anomaly",
        pipeline_stages_run=["sentinel", "investigator"] if investigator else ["sentinel"],
        sentinel=_make_sentinel_finding(),
        investigator=_make_investigator_finding(root_cause=root_cause) if investigator else None,
        cost=IncidentCost(sentinel_llm_calls=0, investigator_turns_or_calls=5, investigator_cost_usd=0.5, wall_clock_seconds=10.0),
    )


@dataclass
class _ScriptedBackend:
    """Duck-typed LLMBackend test double, same convention as
    tests/test_investigator.py's _ScriptedBackend: scripts responses in
    order (a CompletionResult, or an exception INSTANCE to raise), records
    every prompt it was called with."""

    complete_responses: list = field(default_factory=list)
    complete_calls: list = field(default_factory=list)
    _index: int = 0

    def complete(self, messages, tools=None, max_tokens=4096, timeout_s=None):
        self.complete_calls.append(messages[0]["content"])
        response = self.complete_responses[self._index]
        self._index += 1
        if isinstance(response, Exception):
            raise response
        return response


def _sql_response(sql: str) -> CompletionResult:
    return CompletionResult(text=f"Here is the fix:\n```sql\n{sql}\n```\n")


# ===========================================================================
# 1. Pure functions -- no mocking, no I/O at all.
# ===========================================================================


class TestFixShapeSelection:
    def test_introduced_at_selects_claims_target(self):
        assert _fix_target_for("introduced_at:claims") is FIX_TARGET_CLAIMS

    def test_inherited_from_selects_staging_target(self):
        assert _fix_target_for("inherited_from:raw_patients") is FIX_TARGET_STAGING

    def test_inconclusive_has_no_fix(self):
        assert _fix_target_for("inconclusive") is None

    def test_unrecognized_value_has_no_fix(self):
        assert _fix_target_for("something_else:claims") is None


class TestPrTitle:
    def test_introduced_at_title(self):
        title = _pr_title("introduced_at:claims", "INC-20260726T023526Z-unitedhealthcare-diabetes")
        assert title == "Guard claims build: quarantine sign-flipped billing (INC-20260726T023526Z-unitedhealthcare-diabetes)"

    def test_inherited_from_title(self):
        title = _pr_title("inherited_from:raw_patients", "INC-20260724T234736Z-cigna-obesity")
        assert title == "Quarantine invalid source billing at staging boundary (INC-20260724T234736Z-cigna-obesity)"

    def test_titles_are_distinct_for_the_two_fix_shapes(self):
        """The repo owner's explicit demo-legibility ask (Part B amendment
        2): two PRs sitting side by side must read as visibly different."""
        t1 = _pr_title("introduced_at:claims", "INC-1")
        t2 = _pr_title("inherited_from:raw_patients", "INC-1")
        assert t1 != t2

    def test_unknown_root_cause_gets_generic_fallback_title(self):
        assert _pr_title("inconclusive", "INC-1") == "Guardian fix (INC-1)"


class TestExtractSql:
    def test_extracts_single_fenced_block(self):
        text = "Here's my fix:\n```sql\nSELECT 1;\n```\nDone."
        assert _extract_sql(text) == "SELECT 1;"

    def test_uses_last_block_when_multiple_present(self):
        """Same "last, not first" reasoning as investigator.py's
        _extract_fenced_json -- a model that second-guesses itself mid-
        answer may show an earlier draft before its real final answer."""
        text = "Draft:\n```sql\nSELECT 'draft';\n```\nActually, final:\n```sql\nSELECT 'final';\n```"
        assert _extract_sql(text) == "SELECT 'final';"

    def test_strips_surrounding_whitespace(self):
        text = "```sql\n\n  SELECT 1;  \n\n```"
        assert _extract_sql(text) == "SELECT 1;"

    def test_no_fenced_block_raises_value_error(self):
        with pytest.raises(ValueError):
            _extract_sql("I did not use a code block, sorry.")

    def test_empty_text_raises_value_error(self):
        with pytest.raises(ValueError):
            _extract_sql("")


class TestSchemaHelpers:
    def test_schema_fields_extracts_path_and_type(self):
        details = {"schemaMetadata": {"fields": [{"fieldPath": "billing_amount", "nativeDataType": "REAL"}, {"fieldPath": "claim_id", "nativeDataType": "TEXT"}]}}
        assert _schema_fields(details) == [("billing_amount", "REAL"), ("claim_id", "TEXT")]

    def test_schema_fields_missing_schema_returns_empty(self):
        assert _schema_fields({}) == []

    def test_format_schema_renders_table_and_columns(self):
        schema_context = {"claims": [("billing_amount", "REAL")], "raw_patients": [("billing_amount", "TEXT")]}
        rendered = _format_schema(schema_context)
        assert "claims:" in rendered
        assert "  billing_amount REAL" in rendered
        assert "raw_patients:" in rendered
        assert "  billing_amount TEXT" in rendered

    def test_first_owner_name_reads_real_datahub_shape(self):
        """Shape confirmed live against the real DataHub instance:
        ownership.owners[0].owner.name, e.g. "claims_ops_team"."""
        details = {"ownership": {"owners": [{"owner": {"urn": "urn:li:corpGroup:claims_ops_team", "name": "claims_ops_team"}, "type": "DATAOWNER"}]}}
        assert _first_owner_name(details) == "claims_ops_team"

    def test_first_owner_name_no_owners_returns_none(self):
        assert _first_owner_name({}) is None
        assert _first_owner_name({"ownership": {"owners": []}}) is None


class TestBranchAndUrlHelpers:
    def test_branch_name_lowercases_and_prefixes(self):
        assert _branch_name_for("INC-20260726T023526Z-unitedhealthcare-diabetes") == "guardian/fix-inc-20260726t023526z-unitedhealthcare-diabetes"

    def test_incident_github_url_uses_configured_slug(self):
        url = _incident_github_url("INC-1")
        assert url == f"https://github.com/{remediator.MAIN_REPO_GITHUB_SLUG}/blob/main/examples/INC-1/incident.json"


# ===========================================================================
# 2. Prompt / PR-body template tests.
# ===========================================================================


class TestBuildFixPrompt:
    def test_first_attempt_has_no_previous_attempt_section(self):
        finding = _make_investigator_finding()
        prompt = _build_fix_prompt(finding, FIX_TARGET_CLAIMS, "SELECT 1;", {})
        assert "Your previous attempt" not in prompt

    def test_includes_root_cause_and_current_sql_and_schema(self):
        finding = _make_investigator_finding()
        prompt = _build_fix_prompt(finding, FIX_TARGET_CLAIMS, "SELECT * FROM mart_billing;", {"claims": [("billing_amount", "REAL")]})
        assert finding.primary_root_cause in prompt
        assert finding.root_cause_summary in prompt
        assert "SELECT * FROM mart_billing;" in prompt
        assert "billing_amount REAL" in prompt
        assert "ABS()" in prompt or "flip a sign" in prompt  # explicit "do not correct" instruction present

    def test_retry_with_previous_sql_includes_it_and_the_error(self):
        finding = _make_investigator_finding()
        prompt = _build_fix_prompt(
            finding, FIX_TARGET_CLAIMS, "SELECT 1;", {},
            previous_sql="CREATE TABLE claims_new AS SELECT * FROM claims;",
            previous_error="1 row still violates the check",
        )
        assert "Your previous attempt failed. Previous SQL:" in prompt
        assert "CREATE TABLE claims_new AS SELECT * FROM claims;" in prompt
        assert "1 row still violates the check" in prompt

    def test_retry_after_extraction_failure_has_no_empty_code_block(self):
        """previous_sql="" (extraction itself failed) must not render an
        empty ```sql``` block -- a distinct, cleaner message instead."""
        finding = _make_investigator_finding()
        prompt = _build_fix_prompt(
            finding, FIX_TARGET_CLAIMS, "SELECT 1;", {},
            previous_sql="", previous_error="no fenced ```sql``` block found in the model's response",
        )
        assert "before producing usable SQL" in prompt
        assert "```sql\n\n```" not in prompt


class TestBuildPrBody:
    def _validation(self, **overrides):
        from codegen.sql_validation import ValidationResult
        defaults = dict(success=True, original_count=361, clean_count=325, quarantine_count=36, violation_count_in_clean=0)
        defaults.update(overrides)
        return ValidationResult(**defaults)

    def test_includes_incident_and_segment_and_root_cause(self):
        finding = _make_investigator_finding()
        sentinel = _make_sentinel_finding()
        body = _build_pr_body("INC-1", sentinel, finding, FIX_TARGET_CLAIMS, "SELECT 1;", self._validation(), "claims_ops_team")
        assert "INC-1" in body
        assert "UnitedHealthcare" in body
        assert "diabetes" in body
        assert finding.primary_root_cause in body

    def test_includes_quarantine_count_and_conservation_pass(self):
        finding = _make_investigator_finding()
        sentinel = _make_sentinel_finding()
        validation = self._validation(clean_count=325, quarantine_count=36, original_count=361)
        body = _build_pr_body("INC-1", sentinel, finding, FIX_TARGET_CLAIMS, "SELECT 1;", validation, "claims_ops_team")
        assert "36" in body
        assert "PASS" in body

    def test_conservation_fail_shown_when_rows_dropped(self):
        finding = _make_investigator_finding()
        sentinel = _make_sentinel_finding()
        # 325 + 36 != 361 -- a dropped row.
        validation = self._validation(clean_count=325, quarantine_count=35, original_count=361, success=False)
        body = _build_pr_body("INC-1", sentinel, finding, FIX_TARGET_CLAIMS, "SELECT 1;", validation, "claims_ops_team")
        assert "FAIL" in body

    def test_operational_note_includes_owner(self):
        finding = _make_investigator_finding()
        sentinel = _make_sentinel_finding()
        body = _build_pr_body("INC-1", sentinel, finding, FIX_TARGET_CLAIMS, "SELECT 1;", self._validation(), "claims_ops_team")
        assert "Operational note" in body
        assert "claims_ops_team" in body

    def test_operational_note_falls_back_when_owner_unknown(self):
        finding = _make_investigator_finding()
        sentinel = _make_sentinel_finding()
        body = _build_pr_body("INC-1", sentinel, finding, FIX_TARGET_CLAIMS, "SELECT 1;", self._validation(), None)
        assert "unknown" in body.lower()

    def test_quotes_root_cause_summary_verbatim(self):
        """The ONE place free-form model text appears -- a single, clearly
        marked section quoting root_cause_summary exactly, not paraphrased."""
        finding = _make_investigator_finding()
        sentinel = _make_sentinel_finding()
        body = _build_pr_body("INC-1", sentinel, finding, FIX_TARGET_CLAIMS, "SELECT 1;", self._validation(), "claims_ops_team")
        assert finding.root_cause_summary in body

    def test_includes_the_thesis_sentence(self):
        """The repo owner's explicit instruction: this sentence must appear
        in the submission text, including PR body -- not just the design
        doc or walkthrough."""
        finding = _make_investigator_finding()
        sentinel = _make_sentinel_finding()
        body = _build_pr_body("INC-1", sentinel, finding, FIX_TARGET_CLAIMS, "SELECT 1;", self._validation(), "claims_ops_team")
        assert "wrongful-denial incident" in body
        assert "erasing the evidence" in body

    def test_includes_generated_sql(self):
        finding = _make_investigator_finding()
        sentinel = _make_sentinel_finding()
        body = _build_pr_body("INC-1", sentinel, finding, FIX_TARGET_CLAIMS, "CREATE TABLE claims_quarantine AS SELECT 1;", self._validation(), "claims_ops_team")
        assert "CREATE TABLE claims_quarantine AS SELECT 1;" in body


# ===========================================================================
# 3. Retry loop -- REAL apply_and_validate_fix()/scratch_copy() against a
#    tiny synthetic sqlite db, scripted LLM responses.
# ===========================================================================


@pytest.fixture
def claims_db(tmp_path) -> Path:
    """5-row claims-shaped db, 2 negative billing_amount rows -- same
    fixture shape as tests/test_sql_validation.py's source_db, reused here
    so the retry loop is exercised against real, deterministic validation."""
    db_path = tmp_path / "healthcare.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE claims (claim_id TEXT PRIMARY KEY, billing_amount REAL)")
    conn.executemany(
        "INSERT INTO claims VALUES (?, ?)",
        [("CLM-1", 100.0), ("CLM-2", -50.0), ("CLM-3", 200.0), ("CLM-4", -75.0), ("CLM-5", 300.0)],
    )
    conn.commit()
    conn.close()
    return db_path


CORRECT_CLAIMS_FIX_SQL = """
CREATE TABLE claims_new AS SELECT * FROM claims WHERE billing_amount >= 0;
CREATE TABLE claims_quarantine AS SELECT * FROM claims WHERE billing_amount < 0;
DROP TABLE claims;
ALTER TABLE claims_new RENAME TO claims;
"""

INCOMPLETE_CLAIMS_FIX_SQL = """
CREATE TABLE claims_new AS SELECT * FROM claims WHERE billing_amount >= 0 OR claim_id = 'CLM-2';
CREATE TABLE claims_quarantine AS SELECT * FROM claims WHERE billing_amount < 0 AND claim_id != 'CLM-2';
DROP TABLE claims;
ALTER TABLE claims_new RENAME TO claims;
"""


class TestGenerateAndValidate:
    def test_succeeds_on_first_attempt(self, claims_db):
        backend = _ScriptedBackend(complete_responses=[_sql_response(CORRECT_CLAIMS_FIX_SQL)])
        finding = _make_investigator_finding()
        attempts, success = _generate_and_validate(backend, finding, FIX_TARGET_CLAIMS, "-- old sql", {}, claims_db)
        assert success is True
        assert len(attempts) == 1
        assert attempts[0].validation.success is True
        assert attempts[0].validation.quarantine_count == 2

    def test_fails_then_succeeds_on_retry_with_error_fed_back(self, claims_db):
        backend = _ScriptedBackend(complete_responses=[_sql_response(INCOMPLETE_CLAIMS_FIX_SQL), _sql_response(CORRECT_CLAIMS_FIX_SQL)])
        finding = _make_investigator_finding()
        attempts, success = _generate_and_validate(backend, finding, FIX_TARGET_CLAIMS, "-- old sql", {}, claims_db)
        assert success is True
        assert len(attempts) == 2
        assert attempts[0].validation.success is False
        assert attempts[1].validation.success is True
        # The SECOND prompt must actually contain evidence of the first
        # failure -- not just "try again" with no information.
        second_prompt = backend.complete_calls[1]
        assert "Your previous attempt failed" in second_prompt
        assert INCOMPLETE_CLAIMS_FIX_SQL.strip() in second_prompt

    def test_exhausts_all_attempts_and_reports_honest_failure(self, claims_db):
        backend = _ScriptedBackend(complete_responses=[_sql_response(INCOMPLETE_CLAIMS_FIX_SQL)] * 3)
        finding = _make_investigator_finding()
        attempts, success = _generate_and_validate(backend, finding, FIX_TARGET_CLAIMS, "-- old sql", {}, claims_db, max_retries=2)
        assert success is False
        assert len(attempts) == 3  # 1 initial + 2 retries, per DEFAULT_MAX_RETRIES
        assert all(a.validation.success is False for a in attempts)

    def test_backend_error_is_recorded_and_retried_not_raised(self, claims_db):
        backend = _ScriptedBackend(complete_responses=[LLMBackendError("simulated timeout"), _sql_response(CORRECT_CLAIMS_FIX_SQL)])
        finding = _make_investigator_finding()
        attempts, success = _generate_and_validate(backend, finding, FIX_TARGET_CLAIMS, "-- old sql", {}, claims_db)
        assert success is True
        assert len(attempts) == 2
        assert attempts[0].sql == ""
        assert "simulated timeout" in attempts[0].validation.error

    def test_response_with_no_sql_block_is_recorded_and_retried(self, claims_db):
        backend = _ScriptedBackend(complete_responses=[CompletionResult(text="I'm not sure how to fix this."), _sql_response(CORRECT_CLAIMS_FIX_SQL)])
        finding = _make_investigator_finding()
        attempts, success = _generate_and_validate(backend, finding, FIX_TARGET_CLAIMS, "-- old sql", {}, claims_db)
        assert success is True
        assert len(attempts) == 2
        assert attempts[0].validation.success is False


# ===========================================================================
# 4. _existing_pr_url -- the jq/"null" bug fix, checked directly.
# ===========================================================================


class TestExistingPrUrl:
    def test_empty_pr_list_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            remediator.subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=0, stdout="[]\n", stderr=""),
        )
        assert _existing_pr_url("guardian/fix-inc-1") is None

    def test_nonempty_pr_list_returns_first_url(self, monkeypatch):
        monkeypatch.setattr(
            remediator.subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps([{"url": "https://github.com/x/y/pull/7"}]), stderr=""),
        )
        assert _existing_pr_url("guardian/fix-inc-1") == "https://github.com/x/y/pull/7"

    def test_nonzero_returncode_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            remediator.subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="gh: not authenticated"),
        )
        assert _existing_pr_url("guardian/fix-inc-1") is None

    def test_malformed_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            remediator.subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""),
        )
        assert _existing_pr_url("guardian/fix-inc-1") is None


# ===========================================================================
# 5. _open_pr -- mocked git/gh, no real subprocess calls.
# ===========================================================================


class TestOpenPr:
    def test_returns_existing_pr_without_touching_git(self, monkeypatch):
        monkeypatch.setattr(remediator, "_existing_pr_url", lambda branch: "https://github.com/x/y/pull/5")

        def _fail(*a, **kw):
            raise AssertionError("git must never be touched when a PR already exists for this branch")

        monkeypatch.setattr(remediator, "_run_git", _fail)
        url, existed = _open_pr(FIX_TARGET_CLAIMS, "SELECT 1;", "title", "body", "INC-1", "guardian/fix-inc-1")
        assert url == "https://github.com/x/y/pull/5"
        assert existed is True

    def test_creates_new_pr_and_writes_the_fix_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(remediator, "_existing_pr_url", lambda branch: None)
        monkeypatch.setattr(remediator, "DATA_PLATFORM_REPO_PATH", tmp_path)
        (tmp_path / "transform").mkdir()
        (tmp_path / "transform" / "claims.sql").write_text("-- old sql\n")

        git_calls = []

        def _fake_run_git(args, cwd):
            git_calls.append(args)
            return subprocess.CompletedProcess(args=["git"] + args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(remediator, "_run_git", _fake_run_git)
        monkeypatch.setattr(
            remediator.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(args=cmd, returncode=0, stdout="https://github.com/x/y/pull/9\n", stderr=""),
        )

        url, existed = _open_pr(FIX_TARGET_CLAIMS, "SELECT 2;", "my title", "my body", "INC-2", "guardian/fix-inc-2")

        assert url == "https://github.com/x/y/pull/9"
        assert existed is False
        assert (tmp_path / "transform" / "claims.sql").read_text() == "SELECT 2;\n"
        assert git_calls[0] == ["fetch", "origin", "main"]
        assert git_calls[1] == ["checkout", "-B", "guardian/fix-inc-2", "origin/main"]
        assert git_calls[2] == ["add", "transform/claims.sql"]

    def test_falls_back_to_plain_push_when_force_with_lease_fails(self, monkeypatch, tmp_path):
        """A brand-new branch has nothing to "lease" against -- the first
        push (with --force-with-lease) is expected to fail cleanly in that
        case, and a plain push should be tried once before giving up."""
        monkeypatch.setattr(remediator, "_existing_pr_url", lambda branch: None)
        monkeypatch.setattr(remediator, "DATA_PLATFORM_REPO_PATH", tmp_path)
        (tmp_path / "transform").mkdir()
        (tmp_path / "transform" / "claims.sql").write_text("-- old sql\n")

        git_calls = []

        def _fake_run_git(args, cwd):
            git_calls.append(args)
            if args and args[0] == "push" and "--force-with-lease" in args:
                return subprocess.CompletedProcess(args=["git"] + args, returncode=1, stdout="", stderr="stale info")
            return subprocess.CompletedProcess(args=["git"] + args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(remediator, "_run_git", _fake_run_git)
        monkeypatch.setattr(
            remediator.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(args=cmd, returncode=0, stdout="https://github.com/x/y/pull/9\n", stderr=""),
        )

        url, existed = _open_pr(FIX_TARGET_CLAIMS, "SELECT 2;", "title", "body", "INC-2", "guardian/fix-inc-2")
        assert url == "https://github.com/x/y/pull/9"
        push_calls = [c for c in git_calls if c and c[0] == "push"]
        assert len(push_calls) == 2  # the failed --force-with-lease attempt, then the plain retry

    def test_gh_pr_create_failure_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(remediator, "_existing_pr_url", lambda branch: None)
        monkeypatch.setattr(remediator, "DATA_PLATFORM_REPO_PATH", tmp_path)
        (tmp_path / "transform").mkdir()
        (tmp_path / "transform" / "claims.sql").write_text("-- old sql\n")
        monkeypatch.setattr(remediator, "_run_git", lambda args, cwd: subprocess.CompletedProcess(args=["git"] + args, returncode=0, stdout="", stderr=""))
        monkeypatch.setattr(
            remediator.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="permission denied"),
        )
        with pytest.raises(RuntimeError):
            _open_pr(FIX_TARGET_CLAIMS, "SELECT 2;", "title", "body", "INC-2", "guardian/fix-inc-2")


# ===========================================================================
# 6. run_remediator -- full orchestration, mocked schema/owner fetch and
#    mocked PR opening, REAL validation against a synthetic db.
# ===========================================================================


class TestRunRemediatorOrchestration:
    def test_no_investigator_finding_is_a_noop_no_backend_call(self, monkeypatch):
        def _fail(*a, **kw):
            raise AssertionError("fetch_schema_and_owner should never be called with nothing to remediate")

        monkeypatch.setattr(remediator, "fetch_schema_and_owner", _fail)
        incident = _make_incident(investigator=False)
        backend = _ScriptedBackend(complete_responses=[])
        result = run_remediator(incident, backend)
        assert result.status == "no_fix_available"
        assert backend.complete_calls == []

    def test_inconclusive_finding_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(remediator, "fetch_schema_and_owner", lambda ft: (_ for _ in ()).throw(AssertionError("should not be called")))
        incident = _make_incident(root_cause="inconclusive")
        backend = _ScriptedBackend(complete_responses=[])
        result = run_remediator(incident, backend)
        assert result.status == "no_fix_available"
        assert backend.complete_calls == []

    def test_existing_pr_short_circuits_before_any_llm_or_schema_work(self, monkeypatch):
        """The idempotency check's whole point: a rerun against an incident
        that already has an open PR must spend NEITHER an LLM call NOR a
        DataHub read -- checked directly by making both raise if touched."""
        def _fail_schema(*a, **kw):
            raise AssertionError("fetch_schema_and_owner must not be called when a PR already exists")

        monkeypatch.setattr(remediator, "fetch_schema_and_owner", _fail_schema)
        monkeypatch.setattr(remediator, "_existing_pr_url", lambda branch: "https://github.com/x/y/pull/3")
        incident = _make_incident()
        backend = _ScriptedBackend(complete_responses=[])
        result = run_remediator(incident, backend)
        assert result.status == "success"
        assert result.pr_already_existed is True
        assert result.pr_url == "https://github.com/x/y/pull/3"
        assert backend.complete_calls == []

    def test_successful_fix_opens_a_pr(self, monkeypatch, tmp_path, claims_db):
        (tmp_path / "transform").mkdir()
        (tmp_path / "transform" / "claims.sql").write_text("-- old build sql\n")

        monkeypatch.setattr(remediator, "_existing_pr_url", lambda branch: None)
        monkeypatch.setattr(remediator, "fetch_schema_and_owner", lambda ft: ({ft.table_name: [("billing_amount", "REAL")]}, "claims_ops_team"))

        opened = {}

        def _fake_open_pr(fix_target, sql, title, body, incident_id, branch_name):
            opened["title"] = title
            opened["body"] = body
            return "https://github.com/x/y/pull/42", False

        monkeypatch.setattr(remediator, "_open_pr", _fake_open_pr)

        backend = _ScriptedBackend(complete_responses=[_sql_response(CORRECT_CLAIMS_FIX_SQL)])
        incident = _make_incident()
        result = run_remediator(incident, backend, healthcare_db_path=claims_db, data_platform_repo_path=tmp_path)

        assert result.status == "success"
        assert result.pr_url == "https://github.com/x/y/pull/42"
        assert result.owner == "claims_ops_team"
        assert opened["title"] == "Guard claims build: quarantine sign-flipped billing (INC-20260101T000000Z-unitedhealthcare-diabetes)"
        assert "36" not in opened["title"]  # sanity: title is the deterministic template, not stuffed with numbers

    def test_failed_validation_never_opens_a_pr(self, monkeypatch, tmp_path, claims_db):
        (tmp_path / "transform").mkdir()
        (tmp_path / "transform" / "claims.sql").write_text("-- old build sql\n")

        monkeypatch.setattr(remediator, "_existing_pr_url", lambda branch: None)
        monkeypatch.setattr(remediator, "fetch_schema_and_owner", lambda ft: ({}, "claims_ops_team"))

        def _fail_open_pr(*a, **kw):
            raise AssertionError("a PR must never be opened when validation never succeeded")

        monkeypatch.setattr(remediator, "_open_pr", _fail_open_pr)

        backend = _ScriptedBackend(complete_responses=[_sql_response(INCOMPLETE_CLAIMS_FIX_SQL)] * 3)
        incident = _make_incident()
        result = run_remediator(incident, backend, healthcare_db_path=claims_db, data_platform_repo_path=tmp_path)

        assert result.status == "failed_validation"
        assert result.pr_url is None
        assert len(result.attempts) == 3

    def test_inherited_from_targets_staging_patients_not_claims(self, monkeypatch, tmp_path, claims_db):
        """The demo story (Part D): the two root-cause shapes must touch
        DIFFERENT files. This is checked here at the unit level via which
        file fetch_schema_and_owner/current_sql end up reading."""
        (tmp_path / "transform").mkdir()
        (tmp_path / "transform" / "staging_patients.sql").write_text("-- old staging sql\n")

        seen_fix_targets = []
        monkeypatch.setattr(remediator, "_existing_pr_url", lambda branch: None)

        def _fake_fetch(fix_target):
            seen_fix_targets.append(fix_target)
            return {}, "clinical_team"

        monkeypatch.setattr(remediator, "fetch_schema_and_owner", _fake_fetch)
        monkeypatch.setattr(remediator, "_open_pr", lambda *a, **kw: ("https://github.com/x/y/pull/1", False))

        # staging_patients-shaped db+fix, not claims -- reuses a "staging"
        # table so validation still runs against something real.
        conn = sqlite3.connect(str(claims_db))
        conn.execute("CREATE TABLE staging_patients (id TEXT PRIMARY KEY, billing_amount TEXT)")
        conn.executemany("INSERT INTO staging_patients VALUES (?, ?)", [("A", "100.0"), ("B", "-50.0")])
        conn.commit()
        conn.close()
        staging_fix_sql = """
        CREATE TABLE staging_patients_new AS SELECT * FROM staging_patients WHERE CAST(billing_amount AS REAL) >= 0;
        CREATE TABLE staging_patients_quarantine AS SELECT * FROM staging_patients WHERE CAST(billing_amount AS REAL) < 0;
        DROP TABLE staging_patients;
        ALTER TABLE staging_patients_new RENAME TO staging_patients;
        """
        backend = _ScriptedBackend(complete_responses=[_sql_response(staging_fix_sql)])
        incident = _make_incident(root_cause="inherited_from:raw_patients")
        result = run_remediator(incident, backend, healthcare_db_path=claims_db, data_platform_repo_path=tmp_path)

        assert result.status == "success"
        assert seen_fix_targets[0].table_name == "staging_patients"
        assert seen_fix_targets[0].transform_file == "transform/staging_patients.sql"


# ===========================================================================
# 7. Live end-to-end test -- real DataHub, real healthcare.db (via scratch
#    copy), real LLM backend, real PR opened on denial-guardian-data-
#    platform. Excluded by default (pytest.ini).
# ===========================================================================


@pytest.mark.live
def test_live_remediator_against_real_saved_incident():
    """Generates a real fix for the real, already-committed
    INC-20260726T023526Z-unitedhealthcare-diabetes incident
    (introduced_at:claims), validates it against a scratch copy of the real
    healthcare.db, and opens a real PR on denial-guardian-data-platform.
    Run a second time to prove idempotency: the same PR URL comes back,
    with zero LLM calls spent (the branch already exists).
    """
    from agents.llm_backend import get_backend
    from agents.investigator import EvidenceEntry as _EE, InvestigatorFinding as _IF, RootCauseBreakdownEntry as _RCBE

    repo_root = Path(__file__).parent.parent
    incident_path = repo_root / "examples" / "INC-20260726T023526Z-unitedhealthcare-diabetes" / "incident.json"
    assert incident_path.exists(), f"expected real saved incident at {incident_path}"
    data = json.loads(incident_path.read_text())

    s = data["sentinel"]
    sentinel = SentinelFinding(
        segment=Segment(s["segment"]["insurance_provider"], s["segment"]["medical_condition"]),
        segment_claim_count=s["segment_claim_count"], segment_denial_count=s["segment_denial_count"],
        segment_denial_rate=s["segment_denial_rate"], baseline_denial_rate=s["baseline_denial_rate"],
        z_score=s["z_score"], threshold=s["threshold"], method=s["method"], flagged=s["flagged"], summary=s["summary"],
    )
    i = data["investigator"]
    investigator = _IF(
        primary_root_cause=i["primary_root_cause"],
        root_cause_breakdown=[_RCBE(**e) for e in i["root_cause_breakdown"]],
        affected_branch=i["affected_branch"], datasets_checked_and_clean=i["datasets_checked_and_clean"],
        lineage_path_walked=i["lineage_path_walked"],
        evidence=[_EE(**e) for e in i["evidence"]],
        root_cause_summary=i["root_cause_summary"], confidence=i["confidence"],
        backend_used=i["backend_used"], turns_used=i["turns_used"],
    )
    incident = Incident(
        incident_id=data["incident_id"], created_at=data["created_at"], status=data["status"],
        pipeline_stages_run=data["pipeline_stages_run"], sentinel=sentinel, investigator=investigator,
        cost=IncidentCost(**data["cost"]),
    )
    assert investigator.primary_root_cause.startswith("introduced_at:")

    backend = get_backend()
    result1 = run_remediator(incident, backend)

    assert result1.status == "success"
    assert result1.pr_url is not None
    assert result1.fix_target.transform_file == "transform/claims.sql"
    assert result1.attempts, "at least one real generation attempt should be recorded"
    assert result1.attempts[-1].validation.success is True
    assert result1.attempts[-1].validation.quarantine_count is not None and result1.attempts[-1].validation.quarantine_count > 0

    # --- Run 2: idempotency -- same PR, zero LLM spend. ---
    result2 = run_remediator(incident, backend)
    assert result2.pr_url == result1.pr_url
    assert result2.pr_already_existed is True
    assert result2.attempts == []  # no generation happened at all on the rerun
