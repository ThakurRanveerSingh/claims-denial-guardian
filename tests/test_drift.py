"""
Tests for src/agents/drift.py (docs/architecture/lld-sprint3-wp4.md,
docs/decisions/0010-regeneration-non-determinism.md).

No live calls except ONE, @pytest.mark.live, excluded by default
(pytest.ini's `addopts = -m "not live"`) — runs a real feature-list read
against the real local DataHub, writes tag/doc-note/assertions to the real
denial_risk_model MLModel entity, then runs a second time to prove
idempotency (zero duplicates) — same method test_scribe.py's own live test
already established for this project.

Everything else here is pure-function tests (an in-memory sqlite db, no
mocking needed) or mocked-session tests (stdio_client/ClientSession faked,
no real mcp-server-datahub subprocess spawned; DatahubRestEmitter faked, no
real writes attempted) — same two-tier structure test_scribe.py uses.
"""

import json
import sqlite3
from dataclasses import dataclass, field

import pytest

import agents.drift as drift
from agents.drift import (
    DRIFT_TAG_URN,
    BILLING_ZSCORE_CAP,
    PSI_FLAG_THRESHOLD,
    DriftFinding,
    FeatureHealthCheck,
    _apply_drift_tag,
    _assertion_urn_for_feature,
    _build_drift_doc_description,
    _check_billing_zscore_health,
    _check_segment_denial_rate_range,
    _current_tag_urns,
    _ensure_drift_assertion_defined,
    _parse_drift_doc_entries,
    _psi_vs_standard_normal,
    run_drift_check,
    run_drift_writeback,
)


# ===========================================================================
# Shared fixtures / helpers.
# ===========================================================================


def _make_scores_db(rows: list) -> sqlite3.Connection:
    """`rows`: list of (segment_denial_rate, billing_zscore) tuples.
    Matches src/datahub/schema_sprint1.sql's real denial_model_scores
    schema (columns the check functions actually read)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE denial_model_scores (
            score_id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            risk_score REAL NOT NULL,
            segment_denial_rate_feature REAL,
            billing_zscore_feature REAL,
            scored_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO denial_model_scores (claim_id, model_version, risk_score, "
        "segment_denial_rate_feature, billing_zscore_feature, scored_at) VALUES (?, 'v', 0.1, ?, ?, 'now')",
        [(f"CLM-{i:04d}", dr, z) for i, (dr, z) in enumerate(rows)],
    )
    conn.commit()
    return conn


def _make_finding(feature_checks, overall_status="pass", check_id="drift-20260101T000000Z"):
    return DriftFinding(
        check_id=check_id, model_version="toy-denial-risk-v1", checked_at="2026-01-01T00:00:00+00:00",
        feature_checks=feature_checks, overall_status=overall_status,
    )


def _make_check(feature_name="segment_denial_rate", check_type="range_invariant", status="pass"):
    return FeatureHealthCheck(
        feature_name=feature_name, check_type=check_type, documented_expected="[0.0, 1.0]",
        metric_value=0.0, status=status, plain_summary="fabricated summary",
    )


# ===========================================================================
# 1. The checks themselves — pure functions, in-memory db, no mocking.
# ===========================================================================


class TestPsiVsStandardNormal:
    def test_empty_values_returns_zero(self):
        assert _psi_vs_standard_normal([]) == 0.0

    def test_values_drawn_from_standard_normal_shape_score_low(self):
        """Real project data (55,500 per-segment z-scored claims) measured
        PSI ~= 0.038 against theoretical N(0,1) — well under the 0.10
        no-significant-shift convention. A synthetic set built the same
        way (evenly spread across deciles) should score similarly low."""
        from statistics import NormalDist

        nd = NormalDist(0, 1)
        # One value at the center of each of 1000 evenly-spaced percentiles
        # -- a close approximation of a true standard normal sample.
        values = [nd.inv_cdf((i + 0.5) / 1000) for i in range(1000)]
        psi = _psi_vs_standard_normal(values)
        assert psi < PSI_FLAG_THRESHOLD

    def test_degenerate_distribution_scores_high(self):
        """A distribution that does NOT resemble a standard normal at all
        (everything crammed into one decile) must score high — proves the
        check can actually flag something, not just always pass on any
        input. This is the check §2.1 kept specifically because the
        rejected mean/std check could never do this (it's tautologically
        ~0/1 regardless of shape)."""
        values = [0.001] * 500  # all in the same narrow bin near the median
        psi = _psi_vs_standard_normal(values)
        assert psi > PSI_FLAG_THRESHOLD


class TestCheckSegmentDenialRateRange:
    def test_all_in_range_passes(self):
        conn = _make_scores_db([(0.05, 0.1), (0.20, -0.5), (0.0, 0.0), (1.0, 2.0)])
        checks = _check_segment_denial_rate_range(conn)
        assert len(checks) == 1
        c = checks[0]
        assert c.feature_name == "segment_denial_rate"
        assert c.check_type == "range_invariant"
        assert c.status == "pass"
        assert c.metric_value == 0.0
        conn.close()

    def test_out_of_range_value_is_flagged(self):
        """Simulates the ONLY way this check can ever fail: a bug in the
        upstream calculation or corrupted data, not distributional drift
        (LLD §2.2 — the range itself is a mathematical invariant of
        correct code, not an empirical fact)."""
        conn = _make_scores_db([(0.05, 0.1), (1.4, -0.5), (-0.2, 0.0)])  # two out-of-range values
        checks = _check_segment_denial_rate_range(conn)
        c = checks[0]
        assert c.status == "flagged"
        assert c.metric_value == 2.0
        assert "corrupted" in c.plain_summary or "bug" in c.plain_summary
        conn.close()

    def test_plain_summary_has_no_file_paths_or_code_references(self):
        """Same compliance-narrative-leak discipline decision 0009 §4
        already established — checked here at the source, not just in
        reporter.py's own tests, so a leak can never even be constructed."""
        conn = _make_scores_db([(0.05, 0.1)])
        c = _check_segment_denial_rate_range(conn)[0]
        for token in ("docs/", "src/", ".py", "()"):
            assert token not in c.plain_summary
        conn.close()


class TestCheckBillingZscoreHealth:
    def test_returns_two_checks(self):
        conn = _make_scores_db([(0.1, 0.5), (0.1, -1.2)])
        checks = _check_billing_zscore_health(conn)
        assert len(checks) == 2
        assert {c.check_type for c in checks} == {"cap_exceedance", "shape_vs_theoretical"}
        assert all(c.feature_name == "billing_zscore" for c in checks)
        conn.close()

    def test_cap_exceedance_counts_correctly_and_never_flags_in_v1(self):
        """LLD §2.2: no hard flag threshold invented for cap exceedance in
        v1 -- reported plainly, status always "pass" regardless of the
        exceedance rate. Documented as a deliberate choice, not an
        oversight -- this test pins that choice down."""
        rows = [(0.1, 0.0)] * 8 + [(0.1, 5.0), (0.1, -6.0)]  # 2 of 10 exceed the 4.0 cap
        conn = _make_scores_db(rows)
        cap_check = next(c for c in _check_billing_zscore_health(conn) if c.check_type == "cap_exceedance")
        assert cap_check.metric_value == pytest.approx(20.0)  # 2/10 = 20%
        assert cap_check.status == "pass"
        assert str(BILLING_ZSCORE_CAP) in cap_check.documented_expected
        conn.close()

    def test_shape_check_flags_a_degenerate_distribution(self):
        rows = [(0.1, 0.001)] * 500  # all crammed together -- not normal-shaped at all
        conn = _make_scores_db(rows)
        shape_check = next(c for c in _check_billing_zscore_health(conn) if c.check_type == "shape_vs_theoretical")
        assert shape_check.status == "flagged"
        assert shape_check.metric_value > PSI_FLAG_THRESHOLD
        conn.close()

    def test_plain_summaries_have_no_file_paths_or_code_references(self):
        conn = _make_scores_db([(0.1, 0.5), (0.1, -1.2)])
        for c in _check_billing_zscore_health(conn):
            for token in ("docs/", "src/", ".py", "()"):
                assert token not in c.plain_summary
        conn.close()


# ===========================================================================
# 2. Write helpers — pure functions, fake emitter, no I/O.
# ===========================================================================


@dataclass
class _FakeEmitter:
    emitted: list = field(default_factory=list)

    def emit(self, mcpw):
        self.emitted.append(mcpw)


class TestWriteHelpers:
    def test_apply_drift_tag_writes_union_when_not_present(self):
        emitter = _FakeEmitter()
        wrote = _apply_drift_tag(emitter, "urn:li:mlModel:x", {"urn:li:tag:pii"})
        assert wrote is True
        aspect = emitter.emitted[0].aspect
        assert {t.tag for t in aspect.tags} == {"urn:li:tag:pii", DRIFT_TAG_URN}

    def test_apply_drift_tag_skips_when_already_present(self):
        emitter = _FakeEmitter()
        wrote = _apply_drift_tag(emitter, "urn:li:mlModel:x", {DRIFT_TAG_URN})
        assert wrote is False
        assert emitter.emitted == []

    def test_assertion_urn_for_feature_is_stable_and_distinct(self):
        assert _assertion_urn_for_feature("billing_zscore") == "urn:li:assertion:denial_guardian_billing_zscore_health"
        assert _assertion_urn_for_feature("segment_denial_rate") != _assertion_urn_for_feature("billing_zscore")

    def test_ensure_drift_assertion_defined_description_mentions_feature(self):
        emitter = _FakeEmitter()
        _ensure_drift_assertion_defined(
            emitter, "urn:li:assertion:x", "urn:li:dataset:x", "urn:li:schemaField:(urn:li:dataset:x,billing_zscore_feature)",
            "billing_zscore", "[0,1]",
        )
        assert "billing_zscore" in emitter.emitted[0].aspect.description
        assert emitter.emitted[0].aspect.customAssertion.entity == "urn:li:dataset:x"  # dataset, not the MLModel
        assert emitter.emitted[0].aspect.customAssertion.field == "urn:li:schemaField:(urn:li:dataset:x,billing_zscore_feature)"

    def test_build_drift_doc_description_has_check_id_prefix_and_verdicts(self):
        finding = _make_finding([_make_check(status="pass"), _make_check(feature_name="billing_zscore", status="flagged")])
        desc = _build_drift_doc_description(finding)
        assert desc.startswith(f"[{finding.check_id}]")
        assert "pass" in desc and "flagged" in desc

    def test_parse_drift_doc_entries_extracts_check_ids(self):
        institutional_memory = {
            "elements": [
                {"url": "https://x", "description": "[drift-20260101T000000Z] first check", "created": {"time": 1000}},
                {"url": "https://y", "description": "[drift-20260102T000000Z] second check", "created": {"time": 2000}},
                {"url": "https://z", "description": "unrelated note", "created": {}},
            ]
        }
        ids, elements = _parse_drift_doc_entries(institutional_memory)
        assert ids == {"drift-20260101T000000Z", "drift-20260102T000000Z"}
        assert len(elements) == 3  # unrelated note preserved, just not counted


# ===========================================================================
# 3. Full orchestration — mocked MCP session + mocked emitter/graph.
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
    """Same shape as test_scribe.py's own _ScriptedMcpSession -- dispatches
    on (tool_name, urn) to canned responses."""

    def __init__(self, entity_responses: dict, search_responses: dict = None):
        self.entity_responses = entity_responses  # {urn: response dict}
        self.search_responses = search_responses or {}  # {table_name: urn or None}
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


def _patch_drift_mcp(monkeypatch, session):
    monkeypatch.setattr(drift, "stdio_client", lambda params: _FakeStdioClient(params))
    monkeypatch.setattr(drift, "ClientSession", lambda read, write: session)


def _patch_drift_emitter(monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(drift, "DatahubRestEmitter", lambda server, token=None: fake)
    return fake


class _FakeGraph:
    def __init__(self, institutional_memory_by_urn: dict):
        self.institutional_memory_by_urn = institutional_memory_by_urn
        self.calls: list = []

    def execute_graphql(self, query, variables=None):
        urn = (variables or {}).get("urn")
        self.calls.append(urn)
        im = self.institutional_memory_by_urn.get(urn, {"elements": []})
        return {"mlModel": {"institutionalMemory": im}}


def _patch_drift_graph(monkeypatch, institutional_memory_by_urn: dict = None):
    fake_graph = _FakeGraph(institutional_memory_by_urn or {})
    monkeypatch.setattr(drift, "DataHubGraph", lambda config: fake_graph)
    return fake_graph


FEATURE_TABLE_RESPONSE = {
    "featureTableProperties": {
        "mlFeatures": [
            {"urn": "urn:li:mlFeature:(denial_risk_features,segment_denial_rate)"},
            {"urn": "urn:li:mlFeature:(denial_risk_features,billing_zscore)"},
        ]
    }
}


class TestRunDriftCheckOrchestration:
    def test_reads_feature_list_from_datahub_and_dispatches_checks(self, monkeypatch, tmp_path):
        session = _ScriptedMcpSession({drift.FEATURE_TABLE_URN: FEATURE_TABLE_RESPONSE})
        _patch_drift_mcp(monkeypatch, session)

        db_path = tmp_path / "healthcare.db"
        # run_drift_check() opens its own file:...?mode=ro connection, so
        # the fixture must be a real file, not an in-memory db.
        file_conn = sqlite3.connect(db_path)
        file_conn.execute(
            "CREATE TABLE denial_model_scores (score_id INTEGER PRIMARY KEY, claim_id TEXT, "
            "model_version TEXT, risk_score REAL, segment_denial_rate_feature REAL, "
            "billing_zscore_feature REAL, scored_at TEXT)"
        )
        from statistics import NormalDist

        nd = NormalDist(0, 1)
        rows = [(0.1, nd.inv_cdf((i + 0.5) / 200)) for i in range(200)]  # normal-shaped, realistic sample size
        file_conn.executemany(
            "INSERT INTO denial_model_scores (claim_id, model_version, risk_score, "
            "segment_denial_rate_feature, billing_zscore_feature, scored_at) VALUES (?, 'v', 0.1, ?, ?, 'now')",
            [(f"CLM-{i:04d}", dr, z) for i, (dr, z) in enumerate(rows)],
        )
        file_conn.commit()
        file_conn.close()

        finding = run_drift_check(healthcare_db_path=db_path)
        assert finding.model_version == "toy-denial-risk-v1"
        assert finding.check_id.startswith("drift-")
        feature_names = {c.feature_name for c in finding.feature_checks}
        assert feature_names == {"segment_denial_rate", "billing_zscore"}
        assert finding.overall_status == "pass"
        assert [name for name, _ in session.calls] == ["get_entities"]  # exactly one MCP call

    def test_feature_named_by_datahub_with_no_check_implementation_is_flagged_not_skipped(self, monkeypatch, tmp_path):
        """Quarantine-not-hide (decision 0008's discipline, applied here
        too): if DataHub ever lists a feature this module has no check
        for, that must show up as an explicit, flagged finding -- not
        silently vanish from the report."""
        session = _ScriptedMcpSession(
            {
                drift.FEATURE_TABLE_URN: {
                    "featureTableProperties": {
                        "mlFeatures": [{"urn": "urn:li:mlFeature:(denial_risk_features,some_new_feature)"}]
                    }
                }
            }
        )
        _patch_drift_mcp(monkeypatch, session)
        db_path = tmp_path / "healthcare.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE denial_model_scores (score_id INTEGER PRIMARY KEY, claim_id TEXT, "
            "model_version TEXT, risk_score REAL, segment_denial_rate_feature REAL, "
            "billing_zscore_feature REAL, scored_at TEXT)"
        )
        conn.commit()
        conn.close()

        finding = run_drift_check(healthcare_db_path=db_path)
        assert len(finding.feature_checks) == 1
        c = finding.feature_checks[0]
        assert c.feature_name == "some_new_feature"
        assert c.check_type == "unimplemented"
        assert c.status == "flagged"
        assert finding.overall_status == "1 check(s) flagged"


SCORES_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.denial_model_scores,PROD)"


class TestRunDriftWritebackOrchestration:
    MODEL_URN = drift.MODEL_URN

    def test_fresh_entity_gets_tag_doc_and_two_assertions(self, monkeypatch):
        session = _ScriptedMcpSession(
            {
                self.MODEL_URN: {"urn": self.MODEL_URN, "tags": {"tags": []}},
                _assertion_urn_for_feature("segment_denial_rate"): {"urn": "x"},  # no "info" -> doesn't exist yet
                _assertion_urn_for_feature("billing_zscore"): {"urn": "x"},
            },
            search_responses={drift.SCORES_TABLE_NAME: SCORES_DATASET_URN},
        )
        _patch_drift_mcp(monkeypatch, session)
        fake_emitter = _patch_drift_emitter(monkeypatch)
        _patch_drift_graph(monkeypatch)  # empty institutionalMemory -- fresh entity

        finding = _make_finding(
            [_make_check("segment_denial_rate", "range_invariant", "pass"),
             _make_check("billing_zscore", "cap_exceedance", "pass"),
             _make_check("billing_zscore", "shape_vs_theoretical", "pass")]
        )
        result = run_drift_writeback(finding)

        assert result.entity_urn == self.MODEL_URN
        assert result.tag_applied is True
        assert result.doc_note_added is True
        assert len(result.feature_assertions) == 2  # one per FEATURE, not per check
        assert all(ar.assertion_defined and ar.assertion_run_event_emitted for ar in result.feature_assertions)

        # Assertions target the real dataset (denial_model_scores), not the
        # MLModel -- live-verified this session (DataHub rejects a CUSTOM
        # assertion whose entity isn't a dataset).
        assertion_aspects = [item.aspect for item in fake_emitter.emitted if hasattr(item.aspect, "customAssertion")]
        assert len(assertion_aspects) == 2
        for aspect in assertion_aspects:
            assert aspect.customAssertion.entity == SCORES_DATASET_URN
            assert aspect.customAssertion.field.startswith(f"urn:li:schemaField:({SCORES_DATASET_URN},")

    def test_idempotent_rerun_produces_no_duplicate_writes(self, monkeypatch):
        """Same idempotency discipline as Scribe (decision 0007) — a
        second writeback for the SAME check_id must recognize the tag/doc/
        assertion already present rather than duplicate them."""
        finding = _make_finding([_make_check("segment_denial_rate", "range_invariant", "pass")])
        doc_desc = _build_drift_doc_description(finding)
        session = _ScriptedMcpSession(
            {
                self.MODEL_URN: {"urn": self.MODEL_URN, "tags": {"tags": [{"tag": {"urn": DRIFT_TAG_URN}}]}},
                _assertion_urn_for_feature("segment_denial_rate"): {"urn": "x", "info": {}},  # already defined
            },
            search_responses={drift.SCORES_TABLE_NAME: SCORES_DATASET_URN},
        )
        _patch_drift_mcp(monkeypatch, session)
        _patch_drift_emitter(monkeypatch)
        _patch_drift_graph(monkeypatch, {self.MODEL_URN: {"elements": [{"url": "u", "description": doc_desc, "created": {"time": 1}}]}})

        result = run_drift_writeback(finding)
        assert result.tag_already_present is True
        assert result.tag_applied is False
        assert result.doc_note_already_present is True
        assert result.doc_note_added is False
        assert result.feature_assertions[0].assertion_already_defined is True
        assert result.feature_assertions[0].assertion_defined is False
        # The run event itself is still emitted every time (timeseries,
        # not idempotent state) -- same discipline as Scribe's assertion
        # run events, decision 0007.
        assert result.feature_assertions[0].assertion_run_event_emitted is True

    def test_scores_dataset_not_found_degrades_gracefully_tag_and_doc_still_applied(self, monkeypatch):
        """denial_model_scores unresolvable (e.g. not yet ingested) must
        not crash the whole writeback -- the tag/doc-note steps (which
        don't need it) still complete, and the gap is reported rather than
        silently dropped."""
        session = _ScriptedMcpSession({self.MODEL_URN: {"urn": self.MODEL_URN, "tags": {"tags": []}}})  # no search_responses -> dataset unresolvable
        _patch_drift_mcp(monkeypatch, session)
        _patch_drift_emitter(monkeypatch)
        _patch_drift_graph(monkeypatch)

        finding = _make_finding([_make_check("segment_denial_rate", "range_invariant", "pass")])
        result = run_drift_writeback(finding)

        assert result.tag_applied is True
        assert result.doc_note_added is True
        assert result.skipped_reason is not None
        assert result.feature_assertions[0].assertion_defined is False
        assert result.feature_assertions[0].assertion_already_defined is False

    def test_entity_not_found_is_recorded_not_crashed_on(self, monkeypatch):
        """A genuinely nonexistent entity makes the real MCP server's
        get_entities RAISE (ItemNotFoundError), it does not round-trip as
        an empty/bare dict — confirmed live before this test was written,
        not assumed. Simulated here via a session whose call_tool raises
        for MODEL_URN specifically."""

        class _RaisingSession(_ScriptedMcpSession):
            async def call_tool(self, name, arguments):
                if name == "get_entities" and arguments.get("urns") == drift.MODEL_URN:
                    raise RuntimeError("Entity urn:li:mlModel:(...) not found")
                return await super().call_tool(name, arguments)

        session = _RaisingSession({})
        _patch_drift_mcp(monkeypatch, session)
        _patch_drift_emitter(monkeypatch)
        _patch_drift_graph(monkeypatch)

        finding = _make_finding([_make_check()])
        result = run_drift_writeback(finding)
        assert result.entity_urn is None
        assert result.skipped_reason is not None


# ===========================================================================
# 4. Live — real DataHub, excluded by default.
# ===========================================================================


@pytest.mark.live
class TestLiveDriftCheck:
    def test_check_and_writeback_against_real_datahub_is_idempotent(self):
        """Real feature-list read (denial_risk_features), real checks
        against the real healthcare.db, real writeback to the real
        denial_risk_model entity, run TWICE to prove idempotency -- same
        method test_scribe.py's own live test already established."""
        finding = run_drift_check()
        assert finding.model_version == "toy-denial-risk-v1"
        assert {c.feature_name for c in finding.feature_checks} == {"segment_denial_rate", "billing_zscore"}

        first = run_drift_writeback(finding)
        assert first.entity_urn == drift.MODEL_URN
        second = run_drift_writeback(finding)  # SAME finding/check_id -- must short-circuit
        assert second.tag_already_present is True
        assert second.doc_note_already_present is True
        assert all(ar.assertion_already_defined for ar in second.feature_assertions)
