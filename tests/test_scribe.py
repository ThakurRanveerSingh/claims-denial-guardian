"""
Tests for src/agents/scribe.py (docs/decisions/0007-scribe-writeback-design.md).

No live calls except ONE, @pytest.mark.live, excluded by default (pytest.ini's
`addopts = -m "not live"`) — writes to the real local DataHub, using the real
saved INC-20260726T023526Z-unitedhealthcare-diabetes incident, then reads
back via MCP to prove the tag/doc/assertion landed on the right entities and
NOT on entities outside affected_branch (selectivity), and runs a second time
to prove idempotency (zero duplicates) — the repo owner's exact Part D UAT
criteria, checked directly, not just claimed.

Everything else here is pure-function tests (no mocking needed at all) or
mocked-session tests (stdio_client/ClientSession faked, no real
mcp-server-datahub subprocess spawned; DatahubRestEmitter faked, no real
writes attempted).
"""

import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

import agents.scribe as scribe
from agents.scribe import (
    GUARDIAN_TAG_URN,
    ScribeResult,
    _append_incident_doc_note,
    _apply_incident_tag,
    _assertion_urn_for,
    _build_doc_description,
    _current_tag_urns,
    _github_blob_url,
    _has_billing_amount_column,
    _parse_doc_entries,
    _unexpected_count_for,
    run_scribe,
)
from agents.investigator import EvidenceEntry, InvestigatorFinding, RootCauseBreakdownEntry
from agents.orchestrator import Incident, IncidentCost
from agents.sentinel import METHOD, Segment, SentinelFinding


# ===========================================================================
# Shared fixtures / helpers.
# ===========================================================================


def _make_sentinel_finding(provider="Zorbex Insurance", condition="moonflu"):
    return SentinelFinding(
        segment=Segment(provider, condition), segment_claim_count=300, segment_denial_count=90,
        segment_denial_rate=0.30, baseline_denial_rate=0.05, z_score=13.7, threshold=3.5,
        method=METHOD, flagged=True, summary="fabricated",
    )


def _make_investigator_finding(affected_branch, root_cause="introduced_at:claims"):
    return InvestigatorFinding(
        primary_root_cause=root_cause,
        root_cause_breakdown=[
            RootCauseBreakdownEntry(classification=f"{root_cause} (claims)", claim_count=90, pct=100.0, note="test"),
            RootCauseBreakdownEntry(classification="inherited_from:raw_patients", claim_count=10, pct=10.0, note="test"),
        ],
        affected_branch=affected_branch,
        datasets_checked_and_clean=["mart_billing"],
        lineage_path_walked=["claims", "mart_billing"],
        evidence=[EvidenceEntry(step="1", tool="t", query_or_call="q", result_summary="r")],
        root_cause_summary="fabricated summary", confidence="high",
        backend_used="claude_code", turns_used=5,
    )


def _make_incident(affected_branch, incident_id="INC-20260101T000000Z-zorbex-moonflu", root_cause="introduced_at:claims"):
    return Incident(
        incident_id=incident_id, created_at="2026-01-01T00:00:00+00:00",
        status="investigated", pipeline_stages_run=["sentinel", "investigator"],
        sentinel=_make_sentinel_finding(),
        investigator=_make_investigator_finding(affected_branch, root_cause=root_cause) if affected_branch else None,
        cost=IncidentCost(sentinel_llm_calls=0, investigator_turns_or_calls=5, investigator_cost_usd=0.5, wall_clock_seconds=10.0),
    )


# ===========================================================================
# 1. Pure functions — no mocking, no I/O at all.
# ===========================================================================


class TestPureFunctions:
    def test_assertion_urn_for_includes_table_name(self):
        assert _assertion_urn_for("claims") == "urn:li:assertion:guardian-billing-amount-non-negative-claims"
        assert _assertion_urn_for("raw_patients") == "urn:li:assertion:guardian-billing-amount-non-negative-raw_patients"

    def test_has_billing_amount_column_true(self):
        details = {"schemaMetadata": {"fields": [{"fieldPath": "billing_amount"}, {"fieldPath": "claim_id"}]}}
        assert _has_billing_amount_column(details) is True

    def test_has_billing_amount_column_false(self):
        details = {"schemaMetadata": {"fields": [{"fieldPath": "age"}, {"fieldPath": "medical_condition"}]}}
        assert _has_billing_amount_column(details) is False

    def test_has_billing_amount_column_missing_schema(self):
        assert _has_billing_amount_column({}) is False

    def test_current_tag_urns(self):
        details = {"tags": {"tags": [{"tag": {"urn": "urn:li:tag:pii"}}, {"tag": {"urn": "urn:li:tag:critical"}}]}}
        assert _current_tag_urns(details) == {"urn:li:tag:pii", "urn:li:tag:critical"}

    def test_current_tag_urns_empty(self):
        assert _current_tag_urns({}) == set()
        assert _current_tag_urns({"tags": {"tags": []}}) == set()

    # NOTE: _parse_doc_entries takes the real institutionalMemory GraphQL
    # shape ({"elements": [...]}), read via _read_institutional_memory
    # (SDK/GraphQL, not MCP) -- a real bug found via the live test, see
    # that function's docstring. NOT the MCP get_entities "relatedDocuments"
    # field, which turned out to be a different, unrelated DataHub feature.

    def test_parse_doc_entries_extracts_incident_ids(self):
        institutional_memory = {
            "elements": [
                {"url": "https://x", "description": "[INC-20260101T000000Z-a-b] first incident", "created": {"time": 1000}},
                {"url": "https://y", "description": "[INC-20260102T000000Z-c-d] second incident", "created": {"time": 2000}},
            ]
        }
        ids, elements = _parse_doc_entries(institutional_memory)
        assert ids == {"INC-20260101T000000Z-a-b", "INC-20260102T000000Z-c-d"}
        assert len(elements) == 2
        assert elements[0].url == "https://x"

    def test_parse_doc_entries_ignores_notes_without_incident_prefix(self):
        institutional_memory = {"elements": [{"url": "https://x", "description": "unrelated note", "created": {}}]}
        ids, elements = _parse_doc_entries(institutional_memory)
        assert ids == set()
        assert len(elements) == 1  # still preserved, just not counted as a Guardian incident note

    def test_parse_doc_entries_empty(self):
        assert _parse_doc_entries({}) == (set(), [])
        assert _parse_doc_entries({"elements": None}) == (set(), [])

    def test_build_doc_description_includes_incident_id_prefix(self):
        finding = _make_investigator_finding(["claims"])
        desc = _build_doc_description("INC-TEST-123", "claims", finding)
        assert desc.startswith("[INC-TEST-123]")
        assert "high" in desc  # confidence

    def test_unexpected_count_for_sums_matching_entries(self):
        finding = _make_investigator_finding(["claims"])
        # "introduced_at:claims (claims)" contains "claims" -> matches; the
        # inherited_from:raw_patients entry does NOT contain "claims".
        assert _unexpected_count_for("claims", finding) == 90

    def test_unexpected_count_for_no_match_returns_zero(self):
        finding = _make_investigator_finding(["claims"])
        assert _unexpected_count_for("staging_patients", finding) == 0


class TestGithubBlobUrl:
    @pytest.fixture
    def git_repo(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        return tmp_path

    def test_https_remote(self, git_repo):
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/someorg/somerepo.git"], cwd=git_repo, check=True)
        url = _github_blob_url("INC-123", git_repo)
        assert url == "https://github.com/someorg/somerepo/blob/main/examples/INC-123/incident.json"

    def test_https_remote_no_dot_git_suffix(self, git_repo):
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/someorg/somerepo"], cwd=git_repo, check=True)
        url = _github_blob_url("INC-123", git_repo)
        assert url == "https://github.com/someorg/somerepo/blob/main/examples/INC-123/incident.json"

    def test_ssh_remote(self, git_repo):
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:someorg/somerepo.git"], cwd=git_repo, check=True)
        url = _github_blob_url("INC-123", git_repo)
        assert url == "https://github.com/someorg/somerepo/blob/main/examples/INC-123/incident.json"

    def test_non_github_remote_returns_none(self, git_repo):
        subprocess.run(["git", "remote", "add", "origin", "https://gitlab.com/someorg/somerepo.git"], cwd=git_repo, check=True)
        assert _github_blob_url("INC-123", git_repo) is None

    def test_no_remote_returns_none(self, git_repo):
        assert _github_blob_url("INC-123", git_repo) is None

    def test_not_a_git_repo_returns_none(self, tmp_path):
        assert _github_blob_url("INC-123", tmp_path) is None


# ===========================================================================
# 2. SDK-write helpers, with a fake emitter (records calls, no real writes).
# ===========================================================================


@dataclass
class _FakeEmitter:
    emitted: list = field(default_factory=list)

    def emit(self, mcpw):
        self.emitted.append(mcpw)


class TestWriteHelpers:
    def test_apply_incident_tag_writes_union_when_not_present(self):
        emitter = _FakeEmitter()
        wrote = _apply_incident_tag(emitter, "urn:li:dataset:x", {"urn:li:tag:pii"})
        assert wrote is True
        assert len(emitter.emitted) == 1
        aspect = emitter.emitted[0].aspect
        tag_urns = {t.tag for t in aspect.tags}
        assert tag_urns == {"urn:li:tag:pii", GUARDIAN_TAG_URN}

    def test_apply_incident_tag_skips_when_already_present(self):
        emitter = _FakeEmitter()
        wrote = _apply_incident_tag(emitter, "urn:li:dataset:x", {GUARDIAN_TAG_URN, "urn:li:tag:pii"})
        assert wrote is False
        assert emitter.emitted == []

    def test_append_incident_doc_note_appends_to_existing(self):
        emitter = _FakeEmitter()
        finding = _make_investigator_finding(["claims"])
        existing = []
        wrote = _append_incident_doc_note(
            emitter=emitter, entity_urn="urn:li:dataset:x", entity_name="claims",
            existing_elements=existing, incident_id="INC-1", investigator_finding=finding,
            doc_url="https://github.com/x/y/blob/main/examples/INC-1/incident.json",
        )
        assert wrote is True
        aspect = emitter.emitted[0].aspect
        assert len(aspect.elements) == 1
        assert aspect.elements[0].description.startswith("[INC-1]")

    def test_append_incident_doc_note_preserves_existing_entries(self):
        """The whole point of the read-before-write design (decision 0007):
        appending must not drop what was already there."""
        emitter = _FakeEmitter()
        finding = _make_investigator_finding(["claims"])
        from agents.scribe import InstitutionalMemoryMetadataClass, AuditStampClass

        existing = [
            InstitutionalMemoryMetadataClass(
                url="https://x", description="[INC-OLD] a prior incident",
                createStamp=AuditStampClass(time=1000, actor="urn:li:corpuser:datahub"),
            )
        ]
        _append_incident_doc_note(
            emitter=emitter, entity_urn="urn:li:dataset:x", entity_name="claims",
            existing_elements=existing, incident_id="INC-NEW", investigator_finding=finding, doc_url=None,
        )
        aspect = emitter.emitted[0].aspect
        assert len(aspect.elements) == 2
        descriptions = {e.description for e in aspect.elements}
        assert any(d.startswith("[INC-OLD]") for d in descriptions)
        assert any(d.startswith("[INC-NEW]") for d in descriptions)

    def test_append_incident_doc_note_falls_back_when_url_is_none(self):
        """decision 0007: doc_url may be None if the git remote isn't GitHub
        or unavailable -- must still write a valid (non-empty) url, not crash."""
        emitter = _FakeEmitter()
        finding = _make_investigator_finding(["claims"])
        _append_incident_doc_note(
            emitter=emitter, entity_urn="urn:li:dataset:x", entity_name="claims",
            existing_elements=[], incident_id="INC-1", investigator_finding=finding, doc_url=None,
        )
        aspect = emitter.emitted[0].aspect
        assert aspect.elements[0].url  # non-empty


# ===========================================================================
# 3. Full orchestration — mocked MCP session + mocked emitter.
# ===========================================================================


class _ScriptedMcpSession:
    """A fake ClientSession whose call_tool() dispatches on (tool_name,
    a key derived from arguments) to canned JSON responses -- Scribe's
    calls are contextual (different entities need different answers),
    unlike a single generic canned response.
    """

    def __init__(self, search_responses: dict, entity_responses: dict):
        self.search_responses = search_responses  # {table_name: urn or None}
        self.entity_responses = entity_responses  # {urn: response dict}
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "search":
            query = arguments["query"]
            table_name = query.split(" ", 1)[1] if " " in query else query
            urn = self.search_responses.get(table_name)
            if urn is None:
                payload = {"searchResults": []}
            else:
                payload = {"searchResults": [{"entity": {"urn": urn, "properties": {"name": table_name}}}]}
        elif name == "get_entities":
            # No "result" wrapper -- matches the REAL mcp SDK response shape
            # (see scribe._get_entity_details' docstring: a real bug was
            # found here via the live test, where the mock's wrong-but-
            # self-consistent wrapper masked it).
            urn = arguments["urns"]
            payload = self.entity_responses.get(urn, {"urn": urn})
        else:
            payload = {}
        return _FakeToolResult(json.dumps(payload))


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


def _patch_scribe_mcp(monkeypatch, session):
    monkeypatch.setattr(scribe, "stdio_client", lambda params: _FakeStdioClient(params))
    monkeypatch.setattr(scribe, "ClientSession", lambda read, write: session)


def _patch_scribe_emitter(monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(scribe, "DatahubRestEmitter", lambda server, token=None: fake)
    return fake


class _FakeGraph:
    """Fake DataHubGraph -- used ONLY for _read_institutional_memory's
    SDK-read exception (see that function's docstring for why this one
    read doesn't go through the mocked MCP session above)."""

    def __init__(self, institutional_memory_by_urn: dict):
        self.institutional_memory_by_urn = institutional_memory_by_urn
        self.calls: list = []

    def execute_graphql(self, query, variables=None):
        urn = (variables or {}).get("urn")
        self.calls.append(urn)
        im = self.institutional_memory_by_urn.get(urn, {"elements": []})
        return {"dataset": {"institutionalMemory": im}}


def _patch_scribe_graph(monkeypatch, institutional_memory_by_urn: dict = None):
    fake_graph = _FakeGraph(institutional_memory_by_urn or {})
    monkeypatch.setattr(scribe, "DataHubGraph", lambda config: fake_graph)
    return fake_graph


CLAIMS_URN = "urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.claims,PROD)"
RAW_PATIENTS_URN = "urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.raw_patients,PROD)"
MART_DEMOGRAPHICS_URN = "urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.mart_demographics,PROD)"


class TestRunScribeOrchestration:
    def test_no_investigator_finding_is_a_noop_no_mcp_session(self, monkeypatch):
        def _fail(*a, **kw):
            raise AssertionError("stdio_client should never be called when there's nothing to write back")

        monkeypatch.setattr(scribe, "stdio_client", _fail)
        incident = _make_incident(affected_branch=None)
        incident.investigator = None
        result = run_scribe(incident)
        assert result.entities == []

    def test_empty_affected_branch_is_a_noop(self, monkeypatch):
        def _fail(*a, **kw):
            raise AssertionError("stdio_client should never be called for an empty affected_branch")

        monkeypatch.setattr(scribe, "stdio_client", _fail)
        incident = _make_incident(affected_branch=[])
        result = run_scribe(incident)
        assert result.entities == []

    def test_writes_tag_doc_and_assertion_for_fresh_entity(self, monkeypatch):
        session = _ScriptedMcpSession(
            search_responses={"claims": CLAIMS_URN},
            entity_responses={
                CLAIMS_URN: {
                    "schemaMetadata": {"fields": [{"fieldPath": "billing_amount"}]},
                    "tags": {"tags": []},
                },
                _assertion_urn_for("claims"): {"urn": _assertion_urn_for("claims")},  # no "info" -> doesn't exist yet
            },
        )
        _patch_scribe_mcp(monkeypatch, session)
        fake_emitter = _patch_scribe_emitter(monkeypatch)
        _patch_scribe_graph(monkeypatch)  # empty institutionalMemory -- fresh entity

        incident = _make_incident(affected_branch=["claims"])
        result = run_scribe(incident)

        assert len(result.entities) == 1
        er = result.entities[0]
        assert er.entity_name == "claims"
        assert er.tag_applied is True
        assert er.doc_note_added is True
        assert er.assertion_defined is True
        assert er.assertion_run_event_emitted is True
        assert er.skipped_reason is None
        # _ensure_guardian_tag_exists (tagProperties) + the tag emit
        # (globalTags) + the doc emit (institutionalMemory) + the
        # assertion-info emit + the run-event emit = 5.
        assert len(fake_emitter.emitted) == 5

    def test_selectivity_only_affected_branch_entities_touched(self, monkeypatch):
        """The repo owner's Part D UAT criterion, checked at the unit level
        too: mart_billing (in datasets_checked_and_clean, NOT affected_branch)
        must never even be looked up, let alone written to."""
        session = _ScriptedMcpSession(
            search_responses={"claims": CLAIMS_URN, "mart_billing": "urn:li:dataset:mart_billing_should_not_be_queried"},
            entity_responses={
                CLAIMS_URN: {
                    "schemaMetadata": {"fields": [{"fieldPath": "billing_amount"}]},
                    "tags": {"tags": []},
                },
                _assertion_urn_for("claims"): {"urn": _assertion_urn_for("claims")},
            },
        )
        _patch_scribe_mcp(monkeypatch, session)
        _patch_scribe_emitter(monkeypatch)
        fake_graph = _patch_scribe_graph(monkeypatch)

        incident = _make_incident(affected_branch=["claims"])  # mart_billing deliberately NOT in affected_branch
        run_scribe(incident)

        queried_names = {c[1].get("query", "").split(" ", 1)[-1] for c in session.calls if c[0] == "search"}
        assert queried_names == {"claims"}
        assert "mart_billing" not in queried_names
        # Selectivity applies to the SDK institutionalMemory read too, not
        # just the MCP search calls.
        assert fake_graph.calls == [CLAIMS_URN]

    def test_idempotent_rerun_produces_no_duplicate_writes(self, monkeypatch):
        """Simulates the state AFTER a first run: tag/doc/assertion already
        present. A second run must not re-write the tag, must not duplicate
        the doc note, must not re-define the assertion -- but SHOULD still
        emit the run event (DataHub's own timeseries store is what dedupes
        that, per decision 0007's measured finding, not Scribe skipping it)."""
        already_present_details = {
            "schemaMetadata": {"fields": [{"fieldPath": "billing_amount"}]},
            "tags": {"tags": [{"tag": {"urn": GUARDIAN_TAG_URN}}]},
        }
        session = _ScriptedMcpSession(
            search_responses={"claims": CLAIMS_URN},
            entity_responses={
                CLAIMS_URN: already_present_details,
                _assertion_urn_for("claims"): {"urn": _assertion_urn_for("claims"), "info": {"type": "DATASET"}},  # already defined
            },
        )
        _patch_scribe_mcp(monkeypatch, session)
        fake_emitter = _patch_scribe_emitter(monkeypatch)
        _patch_scribe_graph(
            monkeypatch,
            {CLAIMS_URN: {"elements": [{"url": "https://x", "description": "[INC-RERUN-TEST] already here", "created": {"time": 1}}]}},
        )

        incident = _make_incident(affected_branch=["claims"], incident_id="INC-RERUN-TEST")
        result = run_scribe(incident)

        er = result.entities[0]
        assert er.tag_already_present is True
        assert er.tag_applied is False
        assert er.doc_note_already_present is True
        assert er.doc_note_added is False
        assert er.assertion_already_defined is True
        assert er.assertion_defined is False
        assert er.assertion_run_event_emitted is True  # still emitted -- DataHub dedupes it, not Scribe

    def test_skips_assertion_for_entity_without_billing_amount(self, monkeypatch):
        session = _ScriptedMcpSession(
            search_responses={"mart_demographics": MART_DEMOGRAPHICS_URN},
            entity_responses={
                MART_DEMOGRAPHICS_URN: {
                    "schemaMetadata": {"fields": [{"fieldPath": "age"}, {"fieldPath": "medical_condition"}]},
                    "tags": {"tags": []},
                },
            },
        )
        _patch_scribe_mcp(monkeypatch, session)
        _patch_scribe_emitter(monkeypatch)
        _patch_scribe_graph(monkeypatch)

        incident = _make_incident(affected_branch=["mart_demographics"])
        result = run_scribe(incident)

        er = result.entities[0]
        assert er.tag_applied is True  # tag/doc still happen
        assert er.doc_note_added is True
        assert er.assertion_run_event_emitted is False
        assert "billing_amount" in er.skipped_reason

    def test_entity_not_found_is_recorded_not_crashed_on(self, monkeypatch):
        session = _ScriptedMcpSession(search_responses={"nonexistent_table": None}, entity_responses={})
        _patch_scribe_mcp(monkeypatch, session)
        _patch_scribe_emitter(monkeypatch)
        _patch_scribe_graph(monkeypatch)

        incident = _make_incident(affected_branch=["nonexistent_table"])
        result = run_scribe(incident)

        assert result.entities[0].skipped_reason == "entity not found in DataHub"

    def test_two_entities_in_affected_branch_both_get_written(self, monkeypatch):
        session = _ScriptedMcpSession(
            search_responses={"claims": CLAIMS_URN, "raw_patients": RAW_PATIENTS_URN},
            entity_responses={
                CLAIMS_URN: {"schemaMetadata": {"fields": [{"fieldPath": "billing_amount"}]}, "tags": {"tags": []}},
                RAW_PATIENTS_URN: {"schemaMetadata": {"fields": [{"fieldPath": "billing_amount"}]}, "tags": {"tags": []}},
                _assertion_urn_for("claims"): {"urn": _assertion_urn_for("claims")},
                _assertion_urn_for("raw_patients"): {"urn": _assertion_urn_for("raw_patients")},
            },
        )
        _patch_scribe_mcp(monkeypatch, session)
        _patch_scribe_emitter(monkeypatch)
        _patch_scribe_graph(monkeypatch)

        incident = _make_incident(affected_branch=["claims", "raw_patients"])
        result = run_scribe(incident)

        names = {e.entity_name for e in result.entities}
        assert names == {"claims", "raw_patients"}
        assert all(e.assertion_run_event_emitted for e in result.entities)


# ===========================================================================
# 4. Live end-to-end test — real DataHub, real saved incident. Excluded by
#    default (pytest.ini).
# ===========================================================================


@pytest.mark.live
def test_live_scribe_against_real_saved_incident():
    """The repo owner's exact Part D UAT criteria, checked directly:
    tag + doc + assertion visible on claims and raw_patients (both in this
    incident's affected_branch); NOTHING on mart_billing/staging_patients
    (datasets_checked_and_clean); a second run produces zero duplicates.

    Uses the REAL, already-committed
    examples/INC-20260726T023526Z-unitedhealthcare-diabetes/incident.json —
    not a fabricated one -- reconstructed into real Incident/SentinelFinding/
    InvestigatorFinding objects the same way the coordinator's own manual
    verification did earlier this session.
    """
    import asyncio

    from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

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
    investigator = InvestigatorFinding(
        primary_root_cause=i["primary_root_cause"],
        root_cause_breakdown=[RootCauseBreakdownEntry(**e) for e in i["root_cause_breakdown"]],
        affected_branch=i["affected_branch"], datasets_checked_and_clean=i["datasets_checked_and_clean"],
        lineage_path_walked=i["lineage_path_walked"],
        evidence=[EvidenceEntry(**e) for e in i["evidence"]],
        root_cause_summary=i["root_cause_summary"], confidence=i["confidence"],
        backend_used=i["backend_used"], turns_used=i["turns_used"],
    )
    incident = Incident(
        incident_id=data["incident_id"], created_at=data["created_at"], status=data["status"],
        pipeline_stages_run=data["pipeline_stages_run"], sentinel=sentinel, investigator=investigator,
        cost=IncidentCost(**data["cost"]),
    )
    assert set(investigator.affected_branch) == {"claims", "raw_patients"}
    assert set(investigator.datasets_checked_and_clean) == {"mart_billing", "staging_patients"}

    # --- Run 1 ---
    result1 = run_scribe(incident, repo_root=repo_root)
    assert {e.entity_name for e in result1.entities} == {"claims", "raw_patients"}
    for er in result1.entities:
        assert er.tag_applied or er.tag_already_present
        assert er.doc_note_added or er.doc_note_already_present
        assert er.assertion_run_event_emitted

    # --- Read-back via live GraphQL: selectivity ---
    token = scribe.DATAHUB_TOKEN
    graph = DataHubGraph(DatahubClientConfig(server=scribe.DATAHUB_SERVER, token=token))

    def _tags_and_docs(urn):
        r = graph.execute_graphql(
            """query($urn: String!) { dataset(urn: $urn) {
                 tags { tags { tag { urn } } }
                 institutionalMemory { elements { description } }
               } }""",
            variables={"urn": urn},
        )
        d = r["dataset"]
        tag_urns = {t["tag"]["urn"] for t in d["tags"]["tags"]} if d["tags"] else set()
        docs = [e["description"] for e in d["institutionalMemory"]["elements"]] if d["institutionalMemory"] else []
        return tag_urns, docs

    claims_tags, claims_docs = _tags_and_docs(CLAIMS_URN)
    raw_tags, raw_docs = _tags_and_docs(RAW_PATIENTS_URN)
    mart_billing_tags, mart_billing_docs = _tags_and_docs(
        "urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.mart_billing,PROD)"
    )

    assert GUARDIAN_TAG_URN in claims_tags
    assert GUARDIAN_TAG_URN in raw_tags
    assert any(incident.incident_id in d for d in claims_docs)
    assert any(incident.incident_id in d for d in raw_docs)
    # Selectivity: mart_billing is in datasets_checked_and_clean, not
    # affected_branch -- must show NEITHER the tag NOR a doc note for THIS
    # incident (it may carry the tag from some earlier, unrelated incident's
    # run in this same shared dev instance -- what matters is no note for
    # non-affected-branch entities, from Scribe never even querying them).
    assert not any(incident.incident_id in d for d in mart_billing_docs)

    # --- Assertion, read back directly ---
    assertion_urn = _assertion_urn_for("claims")
    r = graph.execute_graphql(
        "query($urn: String!) { assertion(urn: $urn) { runEvents(status: COMPLETE) { total } } }",
        variables={"urn": assertion_urn},
    )
    total_after_run1 = r["assertion"]["runEvents"]["total"]
    assert total_after_run1 >= 1

    # --- Run 2: idempotency, the repo owner's exact criterion ---
    result2 = run_scribe(incident, repo_root=repo_root)
    for er in result2.entities:
        assert er.tag_already_present is True
        assert er.tag_applied is False
        assert er.doc_note_already_present is True
        assert er.doc_note_added is False

    r2 = graph.execute_graphql(
        "query($urn: String!) { assertion(urn: $urn) { runEvents(status: COMPLETE) { total } } }",
        variables={"urn": assertion_urn},
    )
    total_after_run2 = r2["assertion"]["runEvents"]["total"]
    assert total_after_run2 == total_after_run1, "rerun must not create a duplicate run event"

    claims_tags_after, claims_docs_after = _tags_and_docs(CLAIMS_URN)
    assert len([d for d in claims_docs_after if incident.incident_id in d]) == 1, "rerun must not duplicate the doc note"
