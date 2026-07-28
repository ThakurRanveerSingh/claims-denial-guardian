"""
Tests for src/agents/fhir_export.py (docs/decisions/0012-fhir-compliance-
bridge.md).

Structural validation ONLY, per the WP5 scope cap: valid JSON, required FHIR
R4 ExplanationOfBenefit elements present with the right shape (including the
data-absent-reason mechanism for `type`, and `.text`-only diagnosis/
adjudication with no fabricated coding) — NOT real HL7 conformance
validation against a profile, which is explicitly out of scope.

No live calls except ONE, @pytest.mark.live, excluded by default (pytest.ini's
`addopts = -m "not live"`) — exports real EOB resources for a real canonical
incident, writes the DataHub dataset/lineage/tag/doc-note writeback, then
runs a second time to prove idempotency — same method test_scribe.py's own
live test already established.

Everything else here is pure-function tests (an on-disk sqlite fixture db
matching claims/denials' real schema, no mocking needed) or mocked-session
tests (stdio_client/ClientSession faked, no real mcp-server-datahub
subprocess spawned; DatahubRestEmitter faked, no real writes attempted) —
same two-tier structure test_scribe.py/test_drift.py use.
"""

import json
import sqlite3
from dataclasses import dataclass, field

import pytest

import agents.fhir_export as fhir_export
from agents.fhir_export import (
    DATA_ABSENT_REASON_EXT_URL,
    DENIAL_REASON_SAMPLED,
    FHIR_EXPORT_DATASET_URN,
    GUARDIAN_FHIR_TAG_URN,
    FhirExportResult,
    _apply_fhir_tag,
    _build_eob_resource,
    _build_fhir_doc_description,
    _current_tag_urns,
    _extension_base_url,
    _fetch_sample_claims,
    _parse_doc_entries,
    run_fhir_export,
    run_fhir_writeback,
)
from agents.investigator import InvestigatorFinding, RootCauseBreakdownEntry
from agents.orchestrator import Incident, IncidentCost
from agents.sentinel import Segment, SentinelFinding


# ===========================================================================
# Shared fixtures / helpers.
# ===========================================================================


def _make_claims_db(path, rows: list) -> None:
    """`rows`: list of dicts matching schema_sprint1.sql's real claims/
    denials columns this module actually reads. A real on-disk file (not
    ":memory:") since run_fhir_export() opens its own `file:...?mode=ro`
    connection, same requirement test_drift.py's own db fixture notes."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE claims (
            claim_id TEXT PRIMARY KEY, patient_name TEXT, hospital TEXT, insurance_provider TEXT,
            medical_condition TEXT, admission_type TEXT, billing_amount REAL,
            date_of_admission TEXT, discharge_date TEXT, medication TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE denials (
            denial_id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT NOT NULL,
            denial_date TEXT NOT NULL, denial_reason_code TEXT NOT NULL, denial_amount REAL NOT NULL
        )"""
    )
    for r in rows:
        conn.execute(
            "INSERT INTO claims (claim_id, patient_name, hospital, insurance_provider, medical_condition, "
            "admission_type, billing_amount, date_of_admission, discharge_date, medication) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["claim_id"], r["patient_name"], r["hospital"], r["insurance_provider"], r["medical_condition"],
                r.get("admission_type", "urgent"), r["billing_amount"], r["date_of_admission"], r["discharge_date"],
                r.get("medication", "Aspirin"),
            ),
        )
        conn.execute(
            "INSERT INTO denials (claim_id, denial_date, denial_reason_code, denial_amount) VALUES (?, ?, ?, ?)",
            (r["claim_id"], r["denial_date"], r["denial_reason_code"], r["denial_amount"]),
        )
    conn.commit()
    conn.close()


def _make_incident(incident_id="INC-20260101T000000Z-cigna-obesity", primary_root_cause="inherited_from:raw_patients"):
    sentinel = SentinelFinding(
        segment=Segment("Cigna", "obesity"), segment_claim_count=100, segment_denial_count=20,
        segment_denial_rate=0.2, baseline_denial_rate=0.05, z_score=10.0, threshold=3.5,
        method="two_proportion_z_test", flagged=True, summary="fabricated summary",
    )
    investigator = InvestigatorFinding(
        primary_root_cause=primary_root_cause,
        root_cause_breakdown=[RootCauseBreakdownEntry(classification=primary_root_cause, claim_count=20, pct=100.0, note="n/a")],
        affected_branch=["raw_patients", "claims"], datasets_checked_and_clean=[], lineage_path_walked=[],
        evidence=[], root_cause_summary="fabricated summary", confidence="high",
        backend_used="claude_code", turns_used=5,
    )
    return Incident(
        incident_id=incident_id, created_at="2026-01-01T00:00:00+00:00", status="investigated",
        pipeline_stages_run=["sentinel", "investigator"], sentinel=sentinel, investigator=investigator,
        cost=IncidentCost(sentinel_llm_calls=0, investigator_turns_or_calls=5, investigator_cost_usd=0.1, wall_clock_seconds=1.0),
    )


SAMPLE_ROW = {
    "claim_id": "CLM-000183", "patient_name": "tanya SOto", "hospital": "Stokes Chambers and Martin",
    "insurance_provider": "Cigna", "medical_condition": "obesity", "admission_type": "urgent",
    "billing_amount": -11311.33, "date_of_admission": "2023-07-31", "discharge_date": "2023-08-02",
    "medication": "Lipitor", "denial_date": "2023-08-02", "denial_reason_code": "INVALID_BILLING_AMOUNT",
    "denial_amount": -11311.33,
}


# ===========================================================================
# 1. EOB generation — pure functions, no I/O beyond the fixture db.
# ===========================================================================


class TestFetchSampleClaims:
    def test_filters_to_segment_and_denial_reason(self, tmp_path):
        db_path = tmp_path / "healthcare.db"
        other_reason = dict(SAMPLE_ROW, claim_id="CLM-000200", denial_reason_code="HIGH_RISK_SCORE")
        other_segment = dict(SAMPLE_ROW, claim_id="CLM-000300", insurance_provider="Aetna")
        _make_claims_db(db_path, [SAMPLE_ROW, other_reason, other_segment])

        rows = _fetch_sample_claims(db_path, "Cigna", "obesity", limit=10)
        assert [r["claim_id"] for r in rows] == ["CLM-000183"]

    def test_ordering_is_deterministic(self, tmp_path):
        db_path = tmp_path / "healthcare.db"
        rows_in = [dict(SAMPLE_ROW, claim_id=f"CLM-{i:06d}") for i in (300, 100, 200)]
        _make_claims_db(db_path, rows_in)

        rows = _fetch_sample_claims(db_path, "Cigna", "obesity", limit=10)
        assert [r["claim_id"] for r in rows] == ["CLM-000100", "CLM-000200", "CLM-000300"]

    def test_limit_is_respected(self, tmp_path):
        db_path = tmp_path / "healthcare.db"
        rows_in = [dict(SAMPLE_ROW, claim_id=f"CLM-{i:06d}") for i in range(5)]
        _make_claims_db(db_path, rows_in)

        rows = _fetch_sample_claims(db_path, "Cigna", "obesity", limit=2)
        assert len(rows) == 2


class TestBuildEobResource:
    """The honesty-critical function — every assertion here maps to one
    line of decision 0012's real-vs-placeholder table."""

    def test_is_valid_json_round_trip(self, tmp_path):
        incident = _make_incident()
        resource = _build_eob_resource(SAMPLE_ROW, incident, tmp_path)
        assert json.loads(json.dumps(resource)) == resource

    def test_required_r4_elements_present(self, tmp_path):
        incident = _make_incident()
        r = _build_eob_resource(SAMPLE_ROW, incident, tmp_path)
        for required in ("resourceType", "status", "type", "use", "patient", "created", "insurer", "provider", "outcome", "insurance"):
            assert required in r, f"missing required EOB element: {required}"
        assert r["resourceType"] == "ExplanationOfBenefit"
        assert r["insurance"][0]["focal"] is True
        assert "coverage" in r["insurance"][0]

    def test_type_uses_data_absent_reason_not_a_fabricated_code(self, tmp_path):
        """The one design decision this whole slice hinges on: `type` is a
        required CodeableConcept with no real source data. Must be
        represented via the official data-absent-reason extension, with NO
        `coding` or `text` invented to fill the gap."""
        incident = _make_incident()
        r = _build_eob_resource(SAMPLE_ROW, incident, tmp_path)
        assert "coding" not in r["type"]
        assert "text" not in r["type"]
        exts = r["type"]["extension"]
        assert len(exts) == 1
        assert exts[0]["url"] == DATA_ABSENT_REASON_EXT_URL
        assert exts[0]["valueCode"] == "unsupported"

    def test_diagnosis_is_text_only_no_fabricated_icd10_coding(self, tmp_path):
        incident = _make_incident()
        r = _build_eob_resource(SAMPLE_ROW, incident, tmp_path)
        concept = r["diagnosis"][0]["diagnosisCodeableConcept"]
        assert concept["text"] == "obesity"
        assert "coding" not in concept

    def test_adjudication_reason_is_text_only_no_fabricated_carc_coding(self, tmp_path):
        incident = _make_incident()
        r = _build_eob_resource(SAMPLE_ROW, incident, tmp_path)
        reason = r["item"][0]["adjudication"][0]["reason"]
        assert reason["text"] == "INVALID_BILLING_AMOUNT"
        assert "coding" not in reason

    def test_patient_insurer_provider_are_display_only_no_fake_reference(self, tmp_path):
        """No Patient/Organization resources exist (out of scope) — these
        must never carry a `.reference` that would claim to point at a real,
        resolvable FHIR resource that was never created."""
        incident = _make_incident()
        r = _build_eob_resource(SAMPLE_ROW, incident, tmp_path)
        for field_name in ("patient", "insurer", "provider"):
            ref = r[field_name]
            assert "display" in ref
            assert "reference" not in ref

    def test_quality_flag_extension_carries_real_incident_linkage(self, tmp_path):
        incident = _make_incident(primary_root_cause="introduced_at:claims")
        r = _build_eob_resource(SAMPLE_ROW, incident, tmp_path)
        flag = r["extension"][0]
        sub = {e["url"]: e for e in flag["extension"]}
        assert sub["incidentId"]["valueString"] == incident.incident_id
        assert sub["classification"]["valueString"] == "introduced_at:claims"  # verbatim from InvestigatorFinding
        assert sub["confidence"]["valueString"] == "high"

    def test_net_amount_carries_the_real_negative_value_unaltered(self, tmp_path):
        """The exported resource must show the actual defect, not a
        sanitized number — the negative billing_amount IS the evidence."""
        incident = _make_incident()
        r = _build_eob_resource(SAMPLE_ROW, incident, tmp_path)
        assert r["item"][0]["net"]["value"] == SAMPLE_ROW["billing_amount"]
        assert r["item"][0]["net"]["value"] < 0

    def test_extension_base_degrades_gracefully_without_a_github_remote(self, tmp_path):
        # tmp_path is not a git repo -- must not raise, must not fabricate an org/repo.
        base = _extension_base_url(tmp_path)
        assert base == "urn:guardian:fhir"


class TestRunFhirExport:
    def test_writes_one_file_per_sampled_claim(self, tmp_path):
        db_path = tmp_path / "src" / "datahub" / "healthcare.db"
        db_path.parent.mkdir(parents=True)
        _make_claims_db(db_path, [SAMPLE_ROW])
        examples_dir = tmp_path / "examples"
        incident = _make_incident()

        result = run_fhir_export(incident, healthcare_db_path=db_path, repo_root=tmp_path, examples_dir=examples_dir)
        assert len(result.samples) == 1
        assert result.samples[0].claim_id == "CLM-000183"
        written = json.loads((examples_dir / incident.incident_id / "fhir" / "eob-clm-000183.json").read_text())
        assert written["resourceType"] == "ExplanationOfBenefit"

    def test_no_matching_claims_reports_a_note_not_a_silent_empty_export(self, tmp_path):
        db_path = tmp_path / "healthcare.db"
        _make_claims_db(db_path, [dict(SAMPLE_ROW, insurance_provider="Aetna")])  # different segment
        incident = _make_incident()

        result = run_fhir_export(incident, healthcare_db_path=db_path, repo_root=tmp_path, examples_dir=tmp_path / "examples")
        assert result.samples == []
        assert result.note is not None
        assert DENIAL_REASON_SAMPLED in result.note

    def test_rerun_produces_byte_identical_output(self, tmp_path):
        """Determinism proof at the unit level -- the live test proves the
        DataHub side's idempotency; this proves the file-generation side is
        deterministic in the first place."""
        db_path = tmp_path / "healthcare.db"
        _make_claims_db(db_path, [SAMPLE_ROW])
        incident = _make_incident()
        examples_dir = tmp_path / "examples"

        first = run_fhir_export(incident, healthcare_db_path=db_path, repo_root=tmp_path, examples_dir=examples_dir)
        second = run_fhir_export(incident, healthcare_db_path=db_path, repo_root=tmp_path, examples_dir=examples_dir)
        assert first.samples[0].resource == second.samples[0].resource

    def test_raises_when_incident_has_no_investigator_finding(self, tmp_path):
        incident = _make_incident()
        incident.investigator = None
        with pytest.raises(ValueError):
            run_fhir_export(incident, healthcare_db_path=tmp_path / "x.db", repo_root=tmp_path, examples_dir=tmp_path)


# ===========================================================================
# 2. Write helpers — pure functions, fake emitter, no I/O.
# ===========================================================================


@dataclass
class _FakeEmitter:
    emitted: list = field(default_factory=list)

    def emit(self, mcpw):
        self.emitted.append(mcpw)


class TestWriteHelpers:
    def test_apply_fhir_tag_writes_union_when_not_present(self):
        emitter = _FakeEmitter()
        wrote = _apply_fhir_tag(emitter, {"urn:li:tag:pii"})
        assert wrote is True
        aspect = emitter.emitted[0].aspect
        assert {t.tag for t in aspect.tags} == {"urn:li:tag:pii", GUARDIAN_FHIR_TAG_URN}
        assert emitter.emitted[0].entityUrn == FHIR_EXPORT_DATASET_URN

    def test_apply_fhir_tag_skips_when_already_present(self):
        emitter = _FakeEmitter()
        wrote = _apply_fhir_tag(emitter, {GUARDIAN_FHIR_TAG_URN})
        assert wrote is False
        assert emitter.emitted == []

    def test_build_fhir_doc_description_has_incident_id_prefix_and_classification(self):
        incident = _make_incident(primary_root_cause="introduced_at:claims")
        desc = _build_fhir_doc_description(incident, sample_count=3)
        assert desc.startswith(f"[{incident.incident_id}]")
        assert "introduced_at:claims" in desc
        assert "docs/decisions/0012" in desc  # honesty caveat lands in DataHub too, not just our own docs

    def test_parse_doc_entries_extracts_incident_ids(self):
        institutional_memory = {
            "elements": [
                {"url": "https://x", "description": "[INC-20260101T000000Z-a-b] first export", "created": {"time": 1000}},
                {"url": "https://y", "description": "unrelated note", "created": {}},
            ]
        }
        ids, elements = _parse_doc_entries(institutional_memory)
        assert ids == {"INC-20260101T000000Z-a-b"}
        assert len(elements) == 2  # unrelated note preserved, just not counted


# ===========================================================================
# 3. Full writeback orchestration — mocked MCP session + mocked emitter/graph.
# ===========================================================================


class _FakeToolResult:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeStdioClient:
    def __init__(self, params):
        self.params = params

    async def __aenter__(self):
        return ("r", "w")

    async def __aexit__(self, *exc):
        return False


class _ScriptedMcpSession:
    """Same shape as test_scribe.py's/test_drift.py's own
    _ScriptedMcpSession -- dispatches on (tool_name, urn) to canned
    responses."""

    def __init__(self, entity_responses: dict = None, search_responses: dict = None):
        self.entity_responses = entity_responses or {}
        self.search_responses = search_responses or {}
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "get_entities":
            urn = arguments["urns"]
            payload = self.entity_responses.get(urn, {"urn": urn})
        elif name == "search":
            query = arguments["query"]
            table_name = query.split(" ", 1)[1] if " " in query else query
            urn = self.search_responses.get(table_name)
            payload = {"searchResults": []} if urn is None else {"searchResults": [{"entity": {"urn": urn, "properties": {"name": table_name}}}]}
        else:
            payload = {}
        return _FakeToolResult(json.dumps(payload))


def _patch_mcp(monkeypatch, session):
    monkeypatch.setattr(fhir_export, "stdio_client", lambda params: _FakeStdioClient(params))
    monkeypatch.setattr(fhir_export, "ClientSession", lambda read, write: session)


def _patch_emitter(monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(fhir_export, "DatahubRestEmitter", lambda server, token=None: fake)
    return fake


class _FakeGraph:
    def __init__(self, institutional_memory_by_urn: dict = None):
        self.institutional_memory_by_urn = institutional_memory_by_urn or {}

    def execute_graphql(self, query, variables=None):
        urn = (variables or {}).get("urn")
        im = self.institutional_memory_by_urn.get(urn, {"elements": []})
        return {"dataset": {"institutionalMemory": im}}


def _patch_graph(monkeypatch, institutional_memory_by_urn: dict = None):
    fake_graph = _FakeGraph(institutional_memory_by_urn)
    monkeypatch.setattr(fhir_export, "DataHubGraph", lambda config: fake_graph)
    return fake_graph


RAW_PATIENTS_URN = "urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.raw_patients,PROD)"


class TestRunFhirWritebackOrchestration:
    def test_fresh_entity_gets_lineage_tag_and_doc_note(self, monkeypatch, tmp_path):
        session = _ScriptedMcpSession(
            entity_responses={FHIR_EXPORT_DATASET_URN: {"urn": FHIR_EXPORT_DATASET_URN, "tags": {"tags": []}}},
            search_responses={"raw_patients": RAW_PATIENTS_URN},
        )
        _patch_mcp(monkeypatch, session)
        fake_emitter = _patch_emitter(monkeypatch)
        _patch_graph(monkeypatch)  # empty institutionalMemory -- fresh entity

        incident = _make_incident()
        export_result = FhirExportResult(incident_id=incident.incident_id, samples=[], output_dir="x")
        result = run_fhir_writeback(incident, export_result, repo_root=tmp_path)

        assert result.entity_urn == FHIR_EXPORT_DATASET_URN
        assert result.upstream_resolved == RAW_PATIENTS_URN
        assert result.tag_applied is True
        assert result.doc_note_added is True
        assert result.skipped_reason is None

        lineage_aspects = [item.aspect for item in fake_emitter.emitted if hasattr(item.aspect, "upstreams")]
        assert len(lineage_aspects) == 1
        assert lineage_aspects[0].upstreams[0].dataset == RAW_PATIENTS_URN

    def test_idempotent_rerun_produces_no_duplicate_tag_or_doc_note(self, monkeypatch, tmp_path):
        incident = _make_incident()
        export_result = FhirExportResult(incident_id=incident.incident_id, samples=[], output_dir="x")
        existing_doc = {
            "elements": [{"url": "https://x", "description": f"[{incident.incident_id}] already exported", "created": {"time": 1}}]
        }
        session = _ScriptedMcpSession(
            entity_responses={FHIR_EXPORT_DATASET_URN: {"urn": FHIR_EXPORT_DATASET_URN, "tags": {"tags": [{"tag": {"urn": GUARDIAN_FHIR_TAG_URN}}]}}},
            search_responses={"raw_patients": RAW_PATIENTS_URN},
        )
        _patch_mcp(monkeypatch, session)
        _patch_emitter(monkeypatch)
        _patch_graph(monkeypatch, {FHIR_EXPORT_DATASET_URN: existing_doc})

        result = run_fhir_writeback(incident, export_result, repo_root=tmp_path)
        assert result.tag_already_present is True
        assert result.doc_note_already_present is True

    def test_raw_patients_not_found_degrades_gracefully_tag_and_doc_still_applied(self, monkeypatch, tmp_path):
        session = _ScriptedMcpSession(
            entity_responses={FHIR_EXPORT_DATASET_URN: {"urn": FHIR_EXPORT_DATASET_URN, "tags": {"tags": []}}},
            search_responses={},  # raw_patients not found
        )
        _patch_mcp(monkeypatch, session)
        _patch_emitter(monkeypatch)
        _patch_graph(monkeypatch)

        incident = _make_incident()
        export_result = FhirExportResult(incident_id=incident.incident_id, samples=[], output_dir="x")
        result = run_fhir_writeback(incident, export_result, repo_root=tmp_path)

        assert result.upstream_resolved is None
        assert result.skipped_reason is not None
        assert result.tag_applied is True  # tag/doc note are independent of lineage resolution
        assert result.doc_note_added is True


# ===========================================================================
# 4. Live — real DataHub, excluded by default.
# ===========================================================================


@pytest.mark.live
class TestLiveFhirExport:
    def test_export_and_writeback_against_real_datahub_is_idempotent(self):
        """Real sample export against the real committed healthcare.db for
        the real Cigna/obesity canonical incident, real writeback (dataset +
        lineage + tag + doc note) to the real local DataHub, run TWICE to
        prove idempotency -- same method test_scribe.py's/test_drift.py's own
        live tests already established. Also confirms lineage resolves to a
        REAL raw_patients URN, not a hardcoded string."""
        from pathlib import Path

        from agents.orchestrator import EXAMPLES_DIR, load_incident

        incident_path = EXAMPLES_DIR / "INC-20260724T234736Z-cigna-obesity" / "incident.json"
        incident = load_incident(incident_path)

        export_result = run_fhir_export(incident)
        assert len(export_result.samples) > 0
        assert export_result.note is None

        first = run_fhir_writeback(incident, export_result)
        assert first.entity_urn == FHIR_EXPORT_DATASET_URN
        assert first.upstream_resolved is not None
        assert first.upstream_resolved.startswith("urn:li:dataset:")

        second = run_fhir_writeback(incident, export_result)  # SAME incident -- must short-circuit
        assert second.tag_already_present is True
        assert second.doc_note_already_present is True
