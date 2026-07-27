"""
Tests for src/agents/orchestrator.py and src/agents/cli.py
(docs/architecture/lld-sprint2.md §4).

No live calls except ONE, `@pytest.mark.live`, excluded by default (same
pytest.ini convention as tests/test_investigator.py) — the real end-to-end
"cold start" proof: a fresh subprocess invoking the actually-installed
`guardian` command (not a Python function call), against the real local
DataHub + the real committed healthcare.db, scoped to Cigna/obesity via
--segment so it makes exactly one real `claude -p` investigation, plus one
free --dry-run pass.

Everything else here mocks run_sentinel()/run_investigator() at the
orchestrator module level — this file is about Orchestrator's OWN logic
(which segments get investigated, how Incident/status get built, what gets
printed/written), not about re-proving Sentinel or Investigator's own
already-tested behavior.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import agents.orchestrator as orchestrator
from agents.orchestrator import (
    Incident,
    IncidentCost,
    _build_incident,
    _make_incident_id,
    _sentinel_finding_to_dict,
    _slugify,
    load_incident,
    print_dry_run_summary,
    print_incident_summary,
    resume_incident,
    run_guardian,
    write_incident,
)
from agents.investigator import EvidenceEntry, InvestigatorFinding, InvestigatorRunResult, RootCauseBreakdownEntry
from agents.remediator import FixTarget, RemediationAttempt, RemediatorResult
from agents.remediator import FreshBuildResult
from codegen.sql_validation import ValidationResult
from agents.scribe import ScribeEntityResult, ScribeResult
from agents.sentinel import METHOD, Segment, SentinelFinding
import agents.cli as cli


# ===========================================================================
# Shared helpers.
# ===========================================================================


def _make_sentinel_finding(provider="Zorbex Insurance", condition="moonflu", flagged=True, z_score=13.7):
    return SentinelFinding(
        segment=Segment(provider, condition),
        segment_claim_count=300,
        segment_denial_count=90 if flagged else 15,
        segment_denial_rate=0.30 if flagged else 0.05,
        baseline_denial_rate=0.05,
        z_score=z_score,
        threshold=3.5,
        method=METHOD,
        flagged=flagged,
        summary="fabricated",
    )


def _make_investigator_finding(root_cause="introduced_at:claims", confidence="high"):
    return InvestigatorFinding(
        primary_root_cause=root_cause,
        root_cause_breakdown=[RootCauseBreakdownEntry(classification=root_cause, claim_count=90, pct=100.0, note="test")],
        affected_branch=["claims"],
        datasets_checked_and_clean=["mart_billing"],
        lineage_path_walked=["claims", "mart_billing", "staging_patients", "raw_patients"],
        evidence=[EvidenceEntry(step="1", tool="query_healthcare_db", query_or_call="SELECT ...", result_summary="...")],
        root_cause_summary="fabricated summary",
        confidence=confidence,
        backend_used="claude_code",
        turns_used=5,
    )


# ===========================================================================
# 1. Incident / IncidentCost construction, serialization.
# ===========================================================================


class TestIncidentSerialization:
    def test_segment_namedtuple_serializes_with_field_names_not_a_bare_array(self):
        """The real bug this module's own docstring names: dataclasses.asdict()
        preserves NamedTuple as a NamedTuple, and json.dumps() then silently
        flattens it to a bare array, losing field names. Incident.to_dict()
        must not do that."""
        sf = _make_sentinel_finding()
        incident = _build_incident(
            sf,
            investigator_finding=None,
            investigator_cost_usd=None,
            investigator_turns_or_calls=None,
            wall_clock_seconds=0.1,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        d = incident.to_dict()
        assert d["sentinel"]["segment"] == {"insurance_provider": "Zorbex Insurance", "medical_condition": "moonflu"}
        # And the whole thing must actually be JSON-serializable, not just
        # "looks like a dict".
        json.dumps(d)

    def test_sentinel_finding_to_dict_matches_incident_to_dict(self):
        sf = _make_sentinel_finding()
        assert _sentinel_finding_to_dict(sf)["segment"] == {
            "insurance_provider": "Zorbex Insurance",
            "medical_condition": "moonflu",
        }

    def test_investigator_none_when_no_anomaly(self):
        sf = _make_sentinel_finding(flagged=False)
        incident = _build_incident(
            sf,
            investigator_finding=None,
            investigator_cost_usd=None,
            investigator_turns_or_calls=None,
            wall_clock_seconds=0.1,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        assert incident.status == "no_anomaly"
        assert incident.investigator is None
        assert incident.pipeline_stages_run == ["sentinel"]
        assert incident.to_dict()["investigator"] is None

    def test_status_investigated_for_confident_finding(self):
        sf = _make_sentinel_finding()
        incident = _build_incident(
            sf,
            investigator_finding=_make_investigator_finding(root_cause="introduced_at:claims"),
            investigator_cost_usd=0.5,
            investigator_turns_or_calls=5,
            wall_clock_seconds=12.0,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        assert incident.status == "investigated"
        assert incident.pipeline_stages_run == ["sentinel", "investigator"]
        assert incident.cost.investigator_cost_usd == 0.5
        assert incident.cost.investigator_turns_or_calls == 5

    def test_status_inconclusive_when_investigator_could_not_settle(self):
        sf = _make_sentinel_finding()
        incident = _build_incident(
            sf,
            investigator_finding=_make_investigator_finding(root_cause="inconclusive", confidence="low"),
            investigator_cost_usd=0.2,
            investigator_turns_or_calls=12,
            wall_clock_seconds=30.0,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        assert incident.status == "inconclusive"
        assert incident.pipeline_stages_run == ["sentinel", "investigator"]

    def test_sentinel_llm_calls_is_always_zero(self):
        """Sentinel's narrate_fn seam is still completely unwired anywhere
        in this codebase (Slice 1 through Slice 4) — 0 is accurate, not a
        placeholder."""
        sf = _make_sentinel_finding(flagged=False)
        incident = _build_incident(
            sf,
            investigator_finding=None,
            investigator_cost_usd=None,
            investigator_turns_or_calls=None,
            wall_clock_seconds=0.1,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        assert incident.cost.sentinel_llm_calls == 0


class TestSlugAndIncidentId:
    def test_slugify_basic(self):
        assert _slugify("Blue Cross") == "blue-cross"
        assert _slugify("UnitedHealthcare") == "unitedhealthcare"
        assert _slugify("A/B  C") == "a-b-c"

    def test_incident_id_format(self):
        import datetime as dt

        when = dt.datetime(2026, 7, 24, 22, 1, 45, tzinfo=dt.timezone.utc)
        incident_id = _make_incident_id(Segment("UnitedHealthcare", "diabetes"), when)
        assert incident_id == "INC-20260724T220145Z-unitedhealthcare-diabetes"

    def test_incident_id_timestamped_to_the_second_avoids_same_day_collision(self):
        import datetime as dt

        seg = Segment("Cigna", "obesity")
        id1 = _make_incident_id(seg, dt.datetime(2026, 7, 24, 10, 0, 0, tzinfo=dt.timezone.utc))
        id2 = _make_incident_id(seg, dt.datetime(2026, 7, 24, 10, 0, 1, tzinfo=dt.timezone.utc))
        assert id1 != id2


class TestWriteIncident:
    def test_writes_expected_file_shape(self, tmp_path):
        sf = _make_sentinel_finding()
        incident = _build_incident(
            sf,
            investigator_finding=_make_investigator_finding(),
            investigator_cost_usd=0.33,
            investigator_turns_or_calls=4,
            wall_clock_seconds=9.5,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        path = write_incident(incident, examples_dir=tmp_path)

        assert path == tmp_path / incident.incident_id / "incident.json"
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["incident_id"] == incident.incident_id
        assert loaded["status"] == "investigated"
        assert loaded["cost"]["investigator_cost_usd"] == 0.33


class TestLoadIncident:
    def test_round_trips_through_write_incident(self, tmp_path):
        sf = _make_sentinel_finding()
        original = _build_incident(
            sf,
            investigator_finding=_make_investigator_finding(),
            investigator_cost_usd=0.33,
            investigator_turns_or_calls=4,
            wall_clock_seconds=9.5,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        path = write_incident(original, examples_dir=tmp_path)

        loaded = load_incident(path)

        assert loaded.incident_id == original.incident_id
        assert loaded.status == original.status
        assert loaded.sentinel.segment == original.sentinel.segment
        assert loaded.sentinel.z_score == original.sentinel.z_score
        assert loaded.investigator.primary_root_cause == original.investigator.primary_root_cause
        assert loaded.investigator.root_cause_breakdown[0].claim_count == original.investigator.root_cause_breakdown[0].claim_count
        assert loaded.cost.investigator_cost_usd == 0.33

    def test_scribe_and_remediator_round_trip_as_real_objects_not_dicts(self, tmp_path):
        """Reporter (Sprint 3 WP3) needs attribute access on these, e.g.
        `incident.remediator.fix_target.transform_file` -- a dict left
        un-reconstructed would fail that with AttributeError."""
        sf = _make_sentinel_finding()
        original = _build_incident(
            sf, investigator_finding=_make_investigator_finding(), investigator_cost_usd=0.1,
            investigator_turns_or_calls=1, wall_clock_seconds=1.0,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        original.scribe = ScribeResult(
            incident_id=original.incident_id,
            entities=[ScribeEntityResult(entity_name="claims", entity_urn="urn:li:dataset:x", tag_applied=True)],
            doc_url="https://github.com/x/y/blob/main/z",
        )
        original.remediator = RemediatorResult(
            incident_id=original.incident_id, status="success",
            fix_target=FixTarget(transform_file="transform/claims.sql", table_name="claims", upstream_tables=("mart_billing", "mart_demographics")),
            attempts=[
                RemediationAttempt(
                    attempt_number=1, sql="SELECT 1;",
                    validation=ValidationResult(success=True, original_count=10, clean_count=8, quarantine_count=2, violation_count_in_clean=0),
                    fresh_build=FreshBuildResult(success=True),
                )
            ],
            pr_url="https://github.com/x/y/pull/1", pr_already_existed=False, owner="claims_ops_team",
        )
        path = write_incident(original, examples_dir=tmp_path)

        loaded = load_incident(path)

        assert loaded.scribe.doc_url == "https://github.com/x/y/blob/main/z"
        assert loaded.scribe.entities[0].entity_name == "claims"
        assert loaded.scribe.entities[0].tag_applied is True

        assert loaded.remediator.pr_url == "https://github.com/x/y/pull/1"
        assert loaded.remediator.owner == "claims_ops_team"
        assert loaded.remediator.fix_target.transform_file == "transform/claims.sql"  # attribute access, not dict indexing
        assert loaded.remediator.fix_target.upstream_tables == ("mart_billing", "mart_demographics")  # restored as a tuple, not left a list
        assert loaded.remediator.attempts[0].validation.quarantine_count == 2
        assert loaded.remediator.attempts[0].fresh_build.success is True

    def test_no_scribe_or_remediator_stays_none(self, tmp_path):
        sf = _make_sentinel_finding()
        original = _build_incident(
            sf, investigator_finding=_make_investigator_finding(), investigator_cost_usd=0.1,
            investigator_turns_or_calls=1, wall_clock_seconds=1.0,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        path = write_incident(original, examples_dir=tmp_path)
        loaded = load_incident(path)
        assert loaded.scribe is None
        assert loaded.remediator is None

    def test_no_anomaly_incident_has_none_investigator(self, tmp_path):
        sf = _make_sentinel_finding(flagged=False)
        original = _build_incident(
            sf, investigator_finding=None, investigator_cost_usd=None, investigator_turns_or_calls=None,
            wall_clock_seconds=0.1, created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        path = write_incident(original, examples_dir=tmp_path)  # write_incident itself doesn't gate on status -- caller's job normally

        loaded = load_incident(path)
        assert loaded.investigator is None


class TestResumeIncident:
    def _write_saved_incident(self, tmp_path, root_cause="introduced_at:claims"):
        sf = _make_sentinel_finding()
        incident = _build_incident(
            sf, investigator_finding=_make_investigator_finding(root_cause=root_cause),
            investigator_cost_usd=0.5, investigator_turns_or_calls=5, wall_clock_seconds=10.0,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        write_incident(incident, examples_dir=tmp_path)
        return incident.incident_id

    def test_remediate_reuses_the_exact_saved_incident_id(self, monkeypatch, tmp_path):
        """The entire point of this function: the incident_id passed to
        run_remediator() must be byte-identical to the one already saved --
        NOT a freshly minted one -- so _branch_name_for resolves to a
        branch a prior run may have already pushed to."""
        incident_id = self._write_saved_incident(tmp_path)

        seen_incident_ids = []

        def _fake_run_remediator(incident, backend, **kw):
            seen_incident_ids.append(incident.incident_id)
            return RemediatorResult(incident_id=incident.incident_id, status="success", pr_url="https://github.com/x/y/pull/1", pr_already_existed=True)

        monkeypatch.setattr(orchestrator, "run_remediator", _fake_run_remediator)
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())

        result = resume_incident(incident_id, "remediate", examples_dir=tmp_path)

        assert seen_incident_ids == [incident_id]
        assert result.incident_id == incident_id
        assert result.remediator is not None
        assert result.remediator.pr_url == "https://github.com/x/y/pull/1"
        assert "remediator" in result.pipeline_stages_run

    def test_remediate_never_invokes_sentinel_or_investigator(self, monkeypatch, tmp_path):
        incident_id = self._write_saved_incident(tmp_path)

        def _fail(*a, **kw):
            raise AssertionError("resume_incident(stage='remediate') must never re-run Sentinel/Investigator")

        monkeypatch.setattr(orchestrator, "run_sentinel", _fail)
        monkeypatch.setattr(orchestrator, "run_investigator", _fail)
        monkeypatch.setattr(orchestrator, "run_remediator", lambda incident, backend, **kw: RemediatorResult(incident_id=incident.incident_id, status="success"))
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())

        resume_incident(incident_id, "remediate", examples_dir=tmp_path)  # must not raise

    def test_unsupported_stage_raises(self, tmp_path):
        incident_id = self._write_saved_incident(tmp_path)
        with pytest.raises(ValueError, match="unsupported stage"):
            resume_incident(incident_id, "writeback", examples_dir=tmp_path)

    def test_no_investigator_finding_raises(self, monkeypatch, tmp_path):
        sf = _make_sentinel_finding(flagged=False)
        incident = _build_incident(
            sf, investigator_finding=None, investigator_cost_usd=None, investigator_turns_or_calls=None,
            wall_clock_seconds=0.1, created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        write_incident(incident, examples_dir=tmp_path)

        with pytest.raises(ValueError, match="nothing to remediate"):
            resume_incident(incident.incident_id, "remediate", examples_dir=tmp_path)

    def test_backend_passed_through_is_used_directly_no_get_backend_call(self, monkeypatch, tmp_path):
        incident_id = self._write_saved_incident(tmp_path)

        def _fail(*a, **kw):
            raise AssertionError("get_backend() should not be called when a backend is passed explicitly")

        monkeypatch.setattr(orchestrator, "get_backend", _fail)
        monkeypatch.setattr(orchestrator, "run_remediator", lambda incident, backend, **kw: RemediatorResult(incident_id=incident.incident_id, status="success"))

        resume_incident(incident_id, "remediate", examples_dir=tmp_path, backend=object())


# ===========================================================================
# 2. run_guardian() — mocked Sentinel/Investigator.
# ===========================================================================


ALL_FAKE_SEGMENTS = [
    _make_sentinel_finding("Zorbex Insurance", "moonflu", flagged=True, z_score=13.7),
    _make_sentinel_finding("Blarg Health", "sneezies", flagged=False, z_score=-1.0),
    _make_sentinel_finding("Quivo Mutual", "wobblejoint", flagged=False, z_score=-1.5),
]


class TestRunGuardian:
    def test_dry_run_never_constructs_a_backend_or_investigates(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: ALL_FAKE_SEGMENTS)

        def _fail_get_backend(*a, **kw):
            raise AssertionError("get_backend() should never be called in --dry-run mode")

        monkeypatch.setattr(orchestrator, "get_backend", _fail_get_backend)

        def _fail_run_investigator(*a, **kw):
            raise AssertionError("run_investigator() should never be called in --dry-run mode")

        monkeypatch.setattr(orchestrator, "run_investigator", _fail_run_investigator)

        incidents = run_guardian(conn=object(), dry_run=True)

        assert len(incidents) == 3
        assert all(inc.status == "no_anomaly" for inc in incidents)
        assert all(inc.investigator is None for inc in incidents)

    def test_default_mode_investigates_only_flagged_segments(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: ALL_FAKE_SEGMENTS)
        fake_backend = object()
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: fake_backend)
        monkeypatch.setattr(orchestrator, "run_scribe", lambda incident: ScribeResult(incident_id=incident.incident_id))

        investigated_segments = []

        def _fake_run_investigator(backend, finding, conn, max_budget_usd=None):
            investigated_segments.append(finding.segment)
            return InvestigatorRunResult(finding=_make_investigator_finding(), cost_usd=0.1, duration_ms=500.0)

        monkeypatch.setattr(orchestrator, "run_investigator", _fake_run_investigator)

        incidents = run_guardian(conn=object())

        assert investigated_segments == [Segment("Zorbex Insurance", "moonflu")]
        by_segment = {inc.sentinel.segment: inc for inc in incidents}
        assert by_segment[Segment("Zorbex Insurance", "moonflu")].status == "investigated"
        assert by_segment[Segment("Blarg Health", "sneezies")].status == "no_anomaly"
        assert by_segment[Segment("Quivo Mutual", "wobblejoint")].status == "no_anomaly"

    def test_segment_override_forces_investigation_regardless_of_flag(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: ALL_FAKE_SEGMENTS)
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())
        monkeypatch.setattr(orchestrator, "run_scribe", lambda incident: ScribeResult(incident_id=incident.incident_id))

        investigated_segments = []

        def _fake_run_investigator(backend, finding, conn, max_budget_usd=None):
            investigated_segments.append(finding.segment)
            return InvestigatorRunResult(finding=_make_investigator_finding(), cost_usd=0.2, duration_ms=1000.0)

        monkeypatch.setattr(orchestrator, "run_investigator", _fake_run_investigator)

        # Blarg Health/sneezies is NOT flagged, per ALL_FAKE_SEGMENTS above —
        # forcing it via segment= must still investigate it, and must NOT
        # also investigate the genuinely-flagged Zorbex segment (scoped to
        # exactly the forced one).
        incidents = run_guardian(conn=object(), segment=("Blarg Health", "sneezies"))

        assert investigated_segments == [Segment("Blarg Health", "sneezies")]
        by_segment = {inc.sentinel.segment: inc for inc in incidents}
        assert by_segment[Segment("Blarg Health", "sneezies")].status == "investigated"
        # The genuinely-flagged segment is NOT investigated when --segment
        # scopes the run elsewhere.
        assert by_segment[Segment("Zorbex Insurance", "moonflu")].status == "no_anomaly"

    def test_segment_not_found_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: ALL_FAKE_SEGMENTS)
        with pytest.raises(ValueError, match="not found among"):
            run_guardian(conn=object(), segment=("Nonexistent Corp", "madeupitis"))

    def test_backend_constructed_once_and_reused_across_multiple_flagged_segments(self, monkeypatch):
        two_flagged = [
            _make_sentinel_finding("A Insurance", "x", flagged=True),
            _make_sentinel_finding("B Insurance", "y", flagged=True),
        ]
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: two_flagged)
        get_backend_calls = []
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: get_backend_calls.append(name) or object())
        monkeypatch.setattr(orchestrator, "run_scribe", lambda incident: ScribeResult(incident_id=incident.incident_id))
        monkeypatch.setattr(
            orchestrator,
            "run_investigator",
            lambda backend, finding, conn, max_budget_usd=None: InvestigatorRunResult(
                finding=_make_investigator_finding(), cost_usd=0.1, duration_ms=100.0
            ),
        )

        run_guardian(conn=object())

        assert len(get_backend_calls) == 1  # constructed lazily, once, then reused — not once per segment

    def test_max_budget_usd_passed_through_to_investigator(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: [ALL_FAKE_SEGMENTS[0]])
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())
        monkeypatch.setattr(orchestrator, "run_scribe", lambda incident: ScribeResult(incident_id=incident.incident_id))
        received = {}

        def _fake_run_investigator(backend, finding, conn, max_budget_usd=None):
            received["max_budget_usd"] = max_budget_usd
            return InvestigatorRunResult(finding=_make_investigator_finding(), cost_usd=0.1, duration_ms=100.0)

        monkeypatch.setattr(orchestrator, "run_investigator", _fake_run_investigator)

        run_guardian(conn=object(), max_budget_usd=3.5)

        assert received["max_budget_usd"] == 3.5

    def test_no_flagged_segments_investigates_nothing(self, monkeypatch):
        all_clean = [_make_sentinel_finding("A", "x", flagged=False), _make_sentinel_finding("B", "y", flagged=False)]
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: all_clean)

        def _fail(*a, **kw):
            raise AssertionError("should never be called — nothing flagged")

        monkeypatch.setattr(orchestrator, "get_backend", _fail)
        monkeypatch.setattr(orchestrator, "run_investigator", _fail)

        incidents = run_guardian(conn=object())
        assert all(inc.status == "no_anomaly" for inc in incidents)

    def test_writeback_true_calls_scribe_and_records_result(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: [ALL_FAKE_SEGMENTS[0]])
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())
        monkeypatch.setattr(
            orchestrator, "run_investigator",
            lambda backend, finding, conn, max_budget_usd=None: InvestigatorRunResult(
                finding=_make_investigator_finding(), cost_usd=0.1, duration_ms=100.0
            ),
        )
        scribe_calls = []

        def _fake_run_scribe(incident):
            scribe_calls.append(incident.incident_id)
            return ScribeResult(incident_id=incident.incident_id, doc_url="https://example.com/x")

        monkeypatch.setattr(orchestrator, "run_scribe", _fake_run_scribe)

        incidents = run_guardian(conn=object())  # writeback defaults to True

        flagged = [i for i in incidents if i.status == "investigated"][0]
        assert scribe_calls == [flagged.incident_id]
        assert flagged.scribe is not None
        assert flagged.scribe.doc_url == "https://example.com/x"
        assert flagged.pipeline_stages_run == ["sentinel", "investigator", "scribe"]

    def test_writeback_false_never_calls_scribe(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: [ALL_FAKE_SEGMENTS[0]])
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())
        monkeypatch.setattr(
            orchestrator, "run_investigator",
            lambda backend, finding, conn, max_budget_usd=None: InvestigatorRunResult(
                finding=_make_investigator_finding(), cost_usd=0.1, duration_ms=100.0
            ),
        )

        def _fail(*a, **kw):
            raise AssertionError("run_scribe() should never be called when writeback=False")

        monkeypatch.setattr(orchestrator, "run_scribe", _fail)

        incidents = run_guardian(conn=object(), writeback=False)

        flagged = [i for i in incidents if i.status == "investigated"][0]
        assert flagged.scribe is None
        assert flagged.pipeline_stages_run == ["sentinel", "investigator"]

    def test_writeback_false_dry_run_and_no_flagged_never_call_scribe(self, monkeypatch):
        """Redundant with dry_run/no-flagged's own existing never-investigate
        guarantees, but pinned explicitly for Scribe too -- writeback=True
        (the default) must not spawn a real MCP session in either of these
        already-covered no-investigation paths."""
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: ALL_FAKE_SEGMENTS)

        def _fail(*a, **kw):
            raise AssertionError("run_scribe() should never be called with nothing investigated")

        monkeypatch.setattr(orchestrator, "run_scribe", _fail)
        monkeypatch.setattr(orchestrator, "get_backend", _fail)
        monkeypatch.setattr(orchestrator, "run_investigator", _fail)

        run_guardian(conn=object(), dry_run=True)  # writeback still defaults True

    def test_remediate_false_by_default_never_calls_remediator(self, monkeypatch):
        """--remediate is opt-in (decision 0008): a plain run_guardian() call
        must never open a PR."""
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: [ALL_FAKE_SEGMENTS[0]])
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())
        monkeypatch.setattr(
            orchestrator, "run_investigator",
            lambda backend, finding, conn, max_budget_usd=None: InvestigatorRunResult(
                finding=_make_investigator_finding(), cost_usd=0.1, duration_ms=100.0
            ),
        )
        monkeypatch.setattr(orchestrator, "run_scribe", lambda incident: ScribeResult(incident_id=incident.incident_id))

        def _fail(*a, **kw):
            raise AssertionError("run_remediator() should never be called when remediate=False")

        monkeypatch.setattr(orchestrator, "run_remediator", _fail)

        incidents = run_guardian(conn=object())  # remediate defaults to False

        flagged = [i for i in incidents if i.status == "investigated"][0]
        assert flagged.remediator is None
        assert "remediator" not in flagged.pipeline_stages_run

    def test_remediate_true_calls_remediator_and_records_result(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: [ALL_FAKE_SEGMENTS[0]])
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())
        monkeypatch.setattr(
            orchestrator, "run_investigator",
            lambda backend, finding, conn, max_budget_usd=None: InvestigatorRunResult(
                finding=_make_investigator_finding(), cost_usd=0.1, duration_ms=100.0
            ),
        )
        monkeypatch.setattr(orchestrator, "run_scribe", lambda incident: ScribeResult(incident_id=incident.incident_id))

        remediate_calls = []

        def _fake_run_remediator(incident, backend):
            remediate_calls.append(incident.incident_id)
            return RemediatorResult(incident_id=incident.incident_id, status="success", pr_url="https://github.com/x/y/pull/1")

        monkeypatch.setattr(orchestrator, "run_remediator", _fake_run_remediator)

        incidents = run_guardian(conn=object(), remediate=True)

        flagged = [i for i in incidents if i.status == "investigated"][0]
        assert remediate_calls == [flagged.incident_id]
        assert flagged.remediator is not None
        assert flagged.remediator.pr_url == "https://github.com/x/y/pull/1"
        assert flagged.pipeline_stages_run == ["sentinel", "investigator", "scribe", "remediator"]

    def test_remediate_true_with_writeback_false_still_runs_independently(self, monkeypatch):
        """remediate and writeback are independent side effects on
        independent systems (DataHub vs. GitHub) -- --remediate must still
        run Remediator even when --no-writeback skips Scribe."""
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: [ALL_FAKE_SEGMENTS[0]])
        monkeypatch.setattr(orchestrator, "get_backend", lambda name: object())
        monkeypatch.setattr(
            orchestrator, "run_investigator",
            lambda backend, finding, conn, max_budget_usd=None: InvestigatorRunResult(
                finding=_make_investigator_finding(), cost_usd=0.1, duration_ms=100.0
            ),
        )

        def _fail_scribe(*a, **kw):
            raise AssertionError("run_scribe() should never be called when writeback=False")

        monkeypatch.setattr(orchestrator, "run_scribe", _fail_scribe)
        monkeypatch.setattr(
            orchestrator, "run_remediator",
            lambda incident, backend: RemediatorResult(incident_id=incident.incident_id, status="success", pr_url="https://github.com/x/y/pull/2"),
        )

        incidents = run_guardian(conn=object(), writeback=False, remediate=True)

        flagged = [i for i in incidents if i.status == "investigated"][0]
        assert flagged.scribe is None
        assert flagged.remediator is not None
        assert flagged.pipeline_stages_run == ["sentinel", "investigator", "remediator"]

    def test_remediate_true_dry_run_and_no_flagged_never_call_remediator(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "run_sentinel", lambda conn, z_threshold=None: ALL_FAKE_SEGMENTS)

        def _fail(*a, **kw):
            raise AssertionError("run_remediator() should never be called with nothing investigated")

        monkeypatch.setattr(orchestrator, "run_remediator", _fail)
        monkeypatch.setattr(orchestrator, "get_backend", _fail)
        monkeypatch.setattr(orchestrator, "run_investigator", _fail)

        run_guardian(conn=object(), dry_run=True, remediate=True)


# ===========================================================================
# 3. Print output.
# ===========================================================================


class TestPrintOutput:
    def test_incident_summary_includes_lineage_path_walked_line(self, capsys):
        """The repo owner's explicit Slice 4 ask: lineage_path_walked gets
        its own printed line — checked directly, not just assumed present
        because it's part of InvestigatorFinding."""
        sf = _make_sentinel_finding()
        incident = _build_incident(
            sf,
            investigator_finding=_make_investigator_finding(),
            investigator_cost_usd=0.42,
            investigator_turns_or_calls=5,
            wall_clock_seconds=12.3,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        print_incident_summary(incident, written_path=Path("/fake/examples/x/incident.json"))
        out = capsys.readouterr().out

        assert "Lineage path walked:" in out
        assert "claims, mart_billing, staging_patients, raw_patients" in out
        assert incident.incident_id in out
        assert "introduced_at:claims" in out
        assert "Cost: $0.4200" in out
        assert "Wall clock: 12.3s" in out
        assert "Written: /fake/examples/x/incident.json" in out

    def test_dry_run_summary_reports_flagged_segments(self, capsys):
        incidents = [
            _build_incident(
                ALL_FAKE_SEGMENTS[0],
                investigator_finding=None,
                investigator_cost_usd=None,
                investigator_turns_or_calls=None,
                wall_clock_seconds=0.0,
                created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            ),
            _build_incident(
                ALL_FAKE_SEGMENTS[1],
                investigator_finding=None,
                investigator_cost_usd=None,
                investigator_turns_or_calls=None,
                wall_clock_seconds=0.0,
                created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            ),
        ]
        print_dry_run_summary(incidents, forced_segment=None)
        out = capsys.readouterr().out
        assert "Zorbex Insurance / moonflu" in out
        assert "Blarg Health" not in out  # not flagged, shouldn't be listed as "would investigate"

    def test_dry_run_summary_no_anomaly_case(self, capsys):
        incidents = [
            _build_incident(
                ALL_FAKE_SEGMENTS[1],
                investigator_finding=None,
                investigator_cost_usd=None,
                investigator_turns_or_calls=None,
                wall_clock_seconds=0.0,
                created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            )
        ]
        print_dry_run_summary(incidents, forced_segment=None)
        out = capsys.readouterr().out
        assert "No anomaly detected this run." in out


# ===========================================================================
# 4. CLI (agents/cli.py) — main() called directly, no subprocess (except the
#    live test below, which deliberately DOES use a real subprocess).
# ===========================================================================


class TestCli:
    def test_dry_run_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli, "run_guardian", lambda **kw: [
                _build_incident(
                    ALL_FAKE_SEGMENTS[1],
                    investigator_finding=None,
                    investigator_cost_usd=None,
                    investigator_turns_or_calls=None,
                    wall_clock_seconds=0.0,
                    created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                )
            ],
        )
        exit_code = cli.main(["run", "--dry-run"])
        assert exit_code == 0
        assert "dry run" in capsys.readouterr().out.lower()

    def test_no_anomaly_exits_zero_and_writes_nothing(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(
            cli,
            "run_guardian",
            lambda **kw: [
                _build_incident(
                    ALL_FAKE_SEGMENTS[1],
                    investigator_finding=None,
                    investigator_cost_usd=None,
                    investigator_turns_or_calls=None,
                    wall_clock_seconds=0.0,
                    created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                )
            ],
        )
        write_calls = []
        monkeypatch.setattr(cli, "write_incident", lambda inc, examples_dir=None: write_calls.append(inc) or Path("x"))

        exit_code = cli.main(["run"])

        assert exit_code == 0
        assert "No anomaly detected" in capsys.readouterr().out
        assert write_calls == []

    def test_investigated_incident_writes_file_and_exits_zero(self, monkeypatch, capsys, tmp_path):
        incident = _build_incident(
            ALL_FAKE_SEGMENTS[0],
            investigator_finding=_make_investigator_finding(),
            investigator_cost_usd=0.5,
            investigator_turns_or_calls=5,
            wall_clock_seconds=10.0,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        monkeypatch.setattr(cli, "run_guardian", lambda **kw: [incident])
        written = tmp_path / incident.incident_id / "incident.json"
        monkeypatch.setattr(cli, "write_incident", lambda inc, examples_dir=None: written)

        exit_code = cli.main(["run"])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert incident.incident_id in out
        assert str(written) in out

    def test_value_error_from_run_guardian_exits_two(self, monkeypatch, capsys):
        def _raise(**kw):
            raise ValueError("bad --segment value")

        monkeypatch.setattr(cli, "run_guardian", _raise)
        exit_code = cli.main(["run", "--segment", "X,Y"])
        assert exit_code == 2
        assert "bad --segment value" in capsys.readouterr().err

    def test_backend_unavailable_exits_one(self, monkeypatch, capsys):
        from agents.llm_backend import BackendNotAvailableError

        def _raise(**kw):
            raise BackendNotAvailableError("ANTHROPIC_API_KEY is not set")

        monkeypatch.setattr(cli, "run_guardian", _raise)
        exit_code = cli.main(["run"])
        assert exit_code == 1
        assert "backend unavailable" in capsys.readouterr().err

    def test_segment_flag_parses_provider_and_condition(self, monkeypatch):
        received = {}
        monkeypatch.setattr(cli, "run_guardian", lambda **kw: received.update(kw) or [])
        cli.main(["run", "--segment", "Cigna,obesity", "--dry-run"])
        assert received["segment"] == ("Cigna", "obesity")

    def test_malformed_segment_flag_is_rejected_by_argparse(self):
        with pytest.raises(SystemExit):
            cli.main(["run", "--segment", "no-comma-here"])

    def test_max_budget_and_llm_backend_flags_pass_through(self, monkeypatch):
        received = {}
        monkeypatch.setattr(cli, "run_guardian", lambda **kw: received.update(kw) or [])
        cli.main(["run", "--dry-run", "--max-budget-usd", "1.5", "--llm-backend", "anthropic"])
        assert received["max_budget_usd"] == 1.5
        assert received["llm_backend_name"] == "anthropic"

    def test_no_writeback_flag_disables_writeback(self, monkeypatch):
        received = {}
        monkeypatch.setattr(cli, "run_guardian", lambda **kw: received.update(kw) or [])
        cli.main(["run", "--dry-run", "--no-writeback"])
        assert received["writeback"] is False

    def test_writeback_defaults_true_without_the_flag(self, monkeypatch):
        received = {}
        monkeypatch.setattr(cli, "run_guardian", lambda **kw: received.update(kw) or [])
        cli.main(["run", "--dry-run"])
        assert received["writeback"] is True

    def test_remediate_flag_enables_remediate(self, monkeypatch):
        received = {}
        monkeypatch.setattr(cli, "run_guardian", lambda **kw: received.update(kw) or [])
        cli.main(["run", "--dry-run", "--remediate"])
        assert received["remediate"] is True

    def test_remediate_defaults_false_without_the_flag(self, monkeypatch):
        received = {}
        monkeypatch.setattr(cli, "run_guardian", lambda **kw: received.update(kw) or [])
        cli.main(["run", "--dry-run"])
        assert received["remediate"] is False

    def test_resume_command_calls_resume_incident_and_writes_the_file(self, monkeypatch, capsys):
        sf = _make_sentinel_finding()
        incident = _build_incident(
            sf, investigator_finding=_make_investigator_finding(), investigator_cost_usd=0.1,
            investigator_turns_or_calls=1, wall_clock_seconds=1.0,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        received = {}

        def _fake_resume_incident(incident_id, stage, llm_backend_name=None):
            received["incident_id"] = incident_id
            received["stage"] = stage
            return incident

        monkeypatch.setattr(cli, "resume_incident", _fake_resume_incident)
        written = {}
        monkeypatch.setattr(cli, "write_incident", lambda inc, examples_dir: written.setdefault("path", f"{examples_dir}/{inc.incident_id}/incident.json") or f"{examples_dir}/{inc.incident_id}/incident.json")

        exit_code = cli.main(["resume", incident.incident_id, "--stage", "remediate"])

        assert exit_code == 0
        assert received["incident_id"] == incident.incident_id
        assert received["stage"] == "remediate"
        assert "path" in written

    def test_resume_command_unsupported_stage_rejected_by_argparse(self):
        with pytest.raises(SystemExit):
            cli.main(["resume", "INC-1", "--stage", "not-a-real-stage"])

    def test_resume_command_value_error_exits_two(self, monkeypatch, capsys):
        def _raise(*a, **kw):
            raise ValueError("INC-does-not-exist has no InvestigatorFinding")

        monkeypatch.setattr(cli, "resume_incident", _raise)
        exit_code = cli.main(["resume", "INC-does-not-exist", "--stage", "remediate"])
        assert exit_code == 2
        assert "guardian resume" in capsys.readouterr().err


# ===========================================================================
# 5. Live end-to-end test — a fresh subprocess invoking the REAL installed
#    `guardian` command. Excluded by default (pytest.ini).
# ===========================================================================


@pytest.mark.live
def test_live_guardian_cli_cold_start_cigna_obesity():
    """The real "cold start -> Sentinel flags -> Investigator investigates
    -> printed narrative naming the root cause with the lineage path it
    walked" proof, per the repo owner's own Slice 4 ask — run as a genuinely
    fresh OS subprocess invoking the installed `guardian` command (not a
    Python function call in-process), against the real local DataHub + the
    real committed healthcare.db.

    Two subprocess calls, only ONE of which is a live `claude -p` call:
      1. `guardian run --dry-run` — free (zero LLM budget), confirms both
         real seeded segments are flagged, cheaply, as part of "cold start."
      2. `guardian run --segment Cigna,obesity` — the one real investigation
         this test (and this slice) makes. Cigna/obesity, not
         UnitedHealthcare/diabetes: Slice 3 already live-proved the harder
         90/10 case; this gets real coverage of the clean 100%-upstream case
         too, instead of spending money re-proving the same thing twice
         (explicit instruction this slice).
    """
    repo_root = Path(__file__).parent.parent

    dry_run_result = subprocess.run(
        ["guardian", "run", "--dry-run"], capture_output=True, text=True, cwd=str(repo_root), timeout=60
    )
    assert dry_run_result.returncode == 0, dry_run_result.stderr
    assert "Cigna / obesity" in dry_run_result.stdout
    assert "UnitedHealthcare / diabetes" in dry_run_result.stdout

    real_result = subprocess.run(
        ["guardian", "run", "--segment", "Cigna,obesity", "--max-budget-usd", "2.0"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=300,
    )

    print("\n=== LIVE `guardian run --segment Cigna,obesity` stdout ===")
    print(real_result.stdout)
    if real_result.returncode != 0:
        print("=== stderr ===")
        print(real_result.stderr)

    assert real_result.returncode == 0, f"guardian run exited {real_result.returncode}: {real_result.stderr}"

    stdout = real_result.stdout
    assert "Cigna / obesity" in stdout
    assert "Root cause:" in stdout
    assert "inherited_from:raw_patients" in stdout, "expected the clean 100%-upstream ground truth for this segment"
    assert "Lineage path walked:" in stdout
    assert "Written: " in stdout

    # Extract the written incident.json path from stdout and verify its
    # real, on-disk shape.
    written_line = next(line for line in stdout.splitlines() if line.startswith("Written: "))
    incident_path = Path(written_line.removeprefix("Written: ").strip())
    assert incident_path.exists(), f"CLI reported writing {incident_path} but it doesn't exist"

    incident_data = json.loads(incident_path.read_text())
    assert incident_data["status"] == "investigated"
    assert incident_data["sentinel"]["segment"] == {"insurance_provider": "Cigna", "medical_condition": "obesity"}
    assert incident_data["investigator"]["primary_root_cause"] == "inherited_from:raw_patients"
    assert incident_data["cost"]["investigator_cost_usd"] is not None
    assert incident_data["cost"]["wall_clock_seconds"] > 0
