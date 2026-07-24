"""
Tests for src/agents/investigator.py (docs/architecture/lld-sprint2.md §2,
decisions 0004/0005).

No live calls in this file except ONE, explicitly marked `@pytest.mark.live`
and excluded by default (see pytest.ini's `addopts = -m "not live"`) — run it
on purpose with `pytest tests/test_investigator.py -m live`.

Layout:
  1. InvestigatorFinding construction / SUBMIT_FINDING_SCHEMA round-trips —
     pure, no backend, no DB.
  2. query_healthcare_db's read-only enforcement — a REAL, direct proof
     (not just "trust the connection string"): a write attempt against a
     throwaway temp-file DB is shown to actually fail, both via the keyword
     filter and, independently, via the read-only connection itself.
  3. Design A (AnthropicBackend/OllamaBackend shape) — the turn-by-turn loop,
     with `backend.complete()` scripted via a lightweight fake backend (no
     anthropic SDK involved) and the `mcp` SDK's stdio session faked (no
     real `mcp-server-datahub` subprocess spawned).
  4. Design B (ClaudeCodeBackend) — `backend.investigate()` scripted to
     return a canned InvestigationResult; verifies fenced-JSON parsing and
     the malformed/missing-JSON -> inconclusive path.
  5. run_investigator()'s top-level dispatch (decision 0005).
  6. The one live integration test (step 8) — see its own docstring.
"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

import agents.investigator as investigator
from agents.investigator import (
    DATAHUB_MCP_TOOLS,
    DESIGN_B_ALLOWED_TOOLS,
    EvidenceEntry,
    InvestigatorFinding,
    InvestigatorRunResult,
    RootCauseBreakdownEntry,
    _breakdown_from_dicts,
    _completion_to_content_blocks,
    _describe_sentinel_finding,
    _design_a_system_prompt,
    _design_b_task_prompt,
    _dispatch_design_a_tool,
    _estimate_cost_usd,
    _evidence_from_dicts,
    _extract_fenced_json,
    _inconclusive_finding,
    _query_healthcare_db,
    _resolve_db_path,
    finding_from_model_output,
    run_investigator,
)
from agents.llm_backend import (
    BackendCallError,
    BackendTimeoutError,
    BudgetExhaustedError,
    CompletionResult,
    InvestigationResult,
    ToolCall,
)
from agents.sentinel import METHOD, Segment, SentinelFinding


# ===========================================================================
# Shared test fixtures / helpers.
# ===========================================================================


def _make_sentinel_finding(
    provider="Zorbex Insurance", condition="moonflu", z_score=13.7, flagged=True
) -> SentinelFinding:
    """A fabricated SentinelFinding for the mocked tests — deliberately NOT
    one of the real seeded segments, keeping these tests' outcome about
    Investigator's own logic, not about what real data happens to contain
    (the real segments are exercised by the live test, section 6)."""
    return SentinelFinding(
        segment=Segment(provider, condition),
        segment_claim_count=300,
        segment_denial_count=90,
        segment_denial_rate=0.30,
        baseline_denial_rate=0.05,
        z_score=z_score,
        threshold=3.5,
        method=METHOD,
        flagged=flagged,
        summary="fabricated finding for testing",
    )


def _valid_finding_dict(root_cause="introduced_at:claims"):
    """A dict matching SUBMIT_FINDING_SCHEMA exactly — the shape both
    designs are asked to produce."""
    return {
        "primary_root_cause": root_cause,
        "root_cause_breakdown": [{"classification": root_cause, "claim_count": 90, "pct": 100.0, "note": "test"}],
        "affected_branch": ["claims"],
        "datasets_checked_and_clean": ["mart_billing", "staging_patients", "raw_patients"],
        "lineage_path_walked": ["claims", "mart_billing"],
        "evidence": [
            {"step": "1", "tool": "query_healthcare_db", "query_or_call": "SELECT ...", "result_summary": "..."}
        ],
        "root_cause_summary": "test summary",
        "confidence": "high",
    }


@dataclass
class _ScriptedBackend:
    """A minimal, duck-typed LLMBackend test double — not a subclass of the
    real ABC, since Investigator's own code only ever calls
    `.supports_delegated_investigation` / `.complete()` / `.investigate()`
    on whatever it's given, never `isinstance(backend, LLMBackend)`. Scripts
    exactly the responses a test needs, in order, and records every call
    made so tests can assert on how Investigator used the backend.
    """

    name: str
    supports_delegated_investigation: bool
    complete_responses: list = field(default_factory=list)
    investigate_result: Any = None  # an InvestigationResult, or an exception INSTANCE to raise
    complete_calls: list = field(default_factory=list)
    investigate_calls: list = field(default_factory=list)
    _complete_index: int = 0

    def complete(self, messages, tools=None, max_tokens=4096, timeout_s=None):
        self.complete_calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        response = self.complete_responses[self._complete_index]
        self._complete_index += 1
        return response

    def investigate(self, task_prompt, mcp_config_path, allowed_tools, max_budget_usd, timeout_s=None):
        self.investigate_calls.append(
            {
                "task_prompt": task_prompt,
                "mcp_config_path": mcp_config_path,
                "allowed_tools": allowed_tools,
                "max_budget_usd": max_budget_usd,
                "timeout_s": timeout_s,
            }
        )
        if isinstance(self.investigate_result, Exception):
            raise self.investigate_result
        return self.investigate_result


class _FakeCallToolResult:
    def __init__(self, text: str):
        self.content = [_FakeTextBlock(text)]


class _FakeTextBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeClientSession:
    """Stands in for mcp.ClientSession — no real mcp-server-datahub
    subprocess is ever spawned in these tests. Records every call_tool
    invocation for assertions."""

    def __init__(self, read=None, write=None):
        self.read = read
        self.write = write
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return _FakeCallToolResult(json.dumps({"tool": name, "arguments": arguments, "fake": "result"}))


class _FakeStdioClient:
    def __init__(self, server_params):
        self.server_params = server_params

    async def __aenter__(self):
        return ("fake_read_stream", "fake_write_stream")

    async def __aexit__(self, *exc_info):
        return False


def _patch_mcp(monkeypatch, session: Optional[_FakeClientSession] = None):
    """Patch both stdio_client and ClientSession to fakes — used by every
    Design A test that runs the real loop (none of them spawn a real MCP
    subprocess)."""
    session = session or _FakeClientSession()
    monkeypatch.setattr(investigator, "stdio_client", lambda params: _FakeStdioClient(params))
    monkeypatch.setattr(investigator, "ClientSession", lambda read, write: session)
    return session


# ===========================================================================
# 1. InvestigatorFinding construction / SUBMIT_FINDING_SCHEMA round-trips.
# ===========================================================================


class TestFindingConstruction:
    def test_finding_from_model_output_full_round_trip(self):
        data = _valid_finding_dict()
        finding = finding_from_model_output(data, backend_used="claude_code", turns_used=5)

        assert finding.primary_root_cause == "introduced_at:claims"
        assert finding.root_cause_breakdown == [
            RootCauseBreakdownEntry(classification="introduced_at:claims", claim_count=90, pct=100.0, note="test")
        ]
        assert finding.affected_branch == ["claims"]
        assert finding.datasets_checked_and_clean == ["mart_billing", "staging_patients", "raw_patients"]
        assert finding.lineage_path_walked == ["claims", "mart_billing"]
        assert finding.evidence == [
            EvidenceEntry(step="1", tool="query_healthcare_db", query_or_call="SELECT ...", result_summary="...")
        ]
        assert finding.confidence == "high"
        assert finding.backend_used == "claude_code"
        assert finding.turns_used == 5

    def test_finding_from_model_output_requires_primary_root_cause(self):
        data = _valid_finding_dict()
        del data["primary_root_cause"]
        with pytest.raises(KeyError):
            finding_from_model_output(data, backend_used="claude_code", turns_used=1)

    def test_breakdown_from_dicts_is_lenient_about_missing_keys(self):
        # Design B's free-form JSON has no schema enforcement — a slightly
        # off shape (missing "note") should degrade gracefully, not crash.
        result = _breakdown_from_dicts([{"classification": "x", "claim_count": 5, "pct": 50.0}])
        assert result == [RootCauseBreakdownEntry(classification="x", claim_count=5, pct=50.0, note="")]

    def test_evidence_from_dicts_is_lenient_about_missing_keys(self):
        result = _evidence_from_dicts([{"step": "1"}])
        assert result == [EvidenceEntry(step="1", tool="", query_or_call="", result_summary="")]

    def test_to_dict_is_plain_and_json_serializable(self):
        finding = finding_from_model_output(_valid_finding_dict(), backend_used="claude_code", turns_used=3)
        d = finding.to_dict()
        assert isinstance(d, dict)
        json.dumps(d)  # must not raise

    def test_inconclusive_finding_shape(self):
        finding = _inconclusive_finding(
            reason="something went wrong",
            evidence=[EvidenceEntry(step="1", tool="t", query_or_call="q", result_summary="r")],
            backend_used="claude_code",
            turns_used=4,
        )
        assert finding.primary_root_cause == "inconclusive"
        assert finding.confidence == "low"
        assert finding.root_cause_breakdown == []
        assert finding.affected_branch == []
        assert finding.root_cause_summary == "something went wrong"
        assert finding.turns_used == 4


class TestPromptContent:
    def test_design_b_prompt_includes_segment_and_schema(self):
        finding = _make_sentinel_finding()
        prompt = _design_b_task_prompt(finding)
        assert "Zorbex Insurance" in prompt
        assert "moonflu" in prompt
        assert "```json" in prompt
        for tool in DATAHUB_MCP_TOOLS:
            assert tool in prompt
        assert "primary_root_cause" in prompt  # the schema itself is embedded

    def test_design_a_prompt_includes_segment_and_submit_finding_instruction(self):
        finding = _make_sentinel_finding()
        prompt = _design_a_system_prompt(finding)
        assert "Zorbex Insurance" in prompt
        assert "submit_finding" in prompt

    def test_describe_sentinel_finding_includes_z_score(self):
        finding = _make_sentinel_finding(z_score=13.7)
        description = _describe_sentinel_finding(finding)
        assert "13.70" in description


# ===========================================================================
# 2. query_healthcare_db read-only enforcement — a REAL, direct proof.
# ===========================================================================


class TestReadOnlyEnforcement:
    @pytest.fixture
    def temp_db(self, tmp_path):
        """A real, throwaway, file-backed sqlite DB — NOT the committed
        healthcare.db (decision 0002 says that one shouldn't be mutated by
        tests either, and a tiny temp DB is faster). File-backed
        specifically: _resolve_db_path can only produce a real reopenable
        path for a file-backed connection, not ":memory:" (see that
        function's own docstring)."""
        db_file = tmp_path / "throwaway.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE claims (claim_id TEXT PRIMARY KEY, billing_amount REAL)")
        conn.execute("INSERT INTO claims VALUES ('CLM-000001', 100.0)")
        conn.commit()
        yield conn, db_file
        conn.close()

    def test_resolve_db_path_finds_the_real_file(self, temp_db):
        conn, db_file = temp_db
        resolved = _resolve_db_path(conn)
        assert resolved == Path(db_file)

    def test_resolve_db_path_returns_none_for_in_memory(self):
        conn = sqlite3.connect(":memory:")
        assert _resolve_db_path(conn) is None

    def test_select_succeeds(self, temp_db):
        conn, db_file = temp_db
        result = _query_healthcare_db("SELECT * FROM claims", conn, _resolve_db_path(conn))
        assert json.loads(result) == [{"claim_id": "CLM-000001", "billing_amount": 100.0}]

    def test_layer_1_keyword_filter_rejects_update(self, temp_db):
        conn, db_file = temp_db
        result = _query_healthcare_db(
            "UPDATE claims SET billing_amount = -1 WHERE claim_id = 'CLM-000001'", conn, _resolve_db_path(conn)
        )
        assert "only SELECT statements are allowed" in result
        # Confirm the keyword filter's rejection actually prevented the
        # write -- not just that it returned an error string.
        row = conn.execute("SELECT billing_amount FROM claims WHERE claim_id = 'CLM-000001'").fetchone()
        assert row[0] == 100.0

    def test_layer_1_keyword_filter_rejects_drop_and_insert(self, temp_db):
        conn, db_file = temp_db
        db_path = _resolve_db_path(conn)
        assert "only SELECT statements are allowed" in _query_healthcare_db("DROP TABLE claims", conn, db_path)
        assert "only SELECT statements are allowed" in _query_healthcare_db(
            "INSERT INTO claims VALUES ('X', 1)", conn, db_path
        )

    def test_layer_2_readonly_connection_itself_rejects_writes_independent_of_keyword_filter(self, temp_db):
        """The REAL enforcement layer, proven directly: bypass
        _query_healthcare_db's keyword filter entirely (call
        sqlite3.connect(..., mode=ro) exactly the way that function does,
        and attempt a write straight through it) — proving the safety
        property doesn't depend on the keyword filter having no gaps, per
        §2.5's explicit design and this slice's explicit ask to "not just
        trust the connection string, prove it."
        """
        conn, db_file = temp_db
        db_path = _resolve_db_path(conn)
        ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                ro_conn.execute("UPDATE claims SET billing_amount = -999 WHERE claim_id = 'CLM-000001'")
        finally:
            ro_conn.close()

        # And confirm, via the ORIGINAL (writable) connection, that the
        # write genuinely never landed.
        row = conn.execute("SELECT billing_amount FROM claims WHERE claim_id = 'CLM-000001'").fetchone()
        assert row[0] == 100.0

    def test_in_memory_fallback_still_executes_select(self):
        """No second read-only handle is possible for :memory: (see
        _resolve_db_path's docstring) -- confirms the fallback branch still
        works for SELECT, with the keyword filter as its only enforcement
        layer in that specific case."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (42)")
        result = _query_healthcare_db("SELECT * FROM t", conn, None)
        assert json.loads(result) == [{"x": 42}]


# ===========================================================================
# 3. Design A — the turn-by-turn loop (AnthropicBackend/OllamaBackend shape).
# ===========================================================================


class TestDesignALoop:
    @pytest.fixture
    def conn(self):
        c = sqlite3.connect(":memory:")
        yield c
        c.close()

    def test_happy_path_reaches_submit_finding(self, monkeypatch, conn):
        session = _patch_mcp(monkeypatch)
        finding_dict = _valid_finding_dict()
        backend = _ScriptedBackend(
            name="anthropic",
            supports_delegated_investigation=False,
            complete_responses=[
                CompletionResult(
                    text="Checking lineage.",
                    tool_calls=[
                        ToolCall(id="t1", name="datahub_lineage_query", input={"mcp_tool_name": "get_lineage", "arguments": {"urn": "x"}})
                    ],
                    usage={"input_tokens": 500, "output_tokens": 100},
                ),
                CompletionResult(
                    text="Querying the DB.",
                    tool_calls=[ToolCall(id="t2", name="query_healthcare_db", input={"sql": "SELECT 1"})],
                    usage={"input_tokens": 500, "output_tokens": 100},
                ),
                CompletionResult(
                    text="Done.",
                    tool_calls=[ToolCall(id="t3", name="submit_finding", input=finding_dict)],
                    usage={"input_tokens": 500, "output_tokens": 100},
                ),
            ],
        )

        result = run_investigator(backend, _make_sentinel_finding(), conn, max_turns=12, max_budget_usd=5.0)

        assert isinstance(result, InvestigatorRunResult)
        assert result.finding.primary_root_cause == "introduced_at:claims"
        assert result.finding.turns_used == 3
        assert result.finding.backend_used == "anthropic"
        # The DataHub relay actually dispatched to the (fake) MCP session.
        assert session.calls == [("get_lineage", {"urn": "x"})]
        # complete() was called with the tool schemas attached every turn.
        assert all(call["tools"] is not None for call in backend.complete_calls)
        # The cost-plumbing fix (Slice 4): three turns of 500/100 tokens each
        # at the module's own ESTIMATED_*_COST_PER_MTOK rates — a real,
        # non-zero running total actually reached the caller now, not
        # silently dropped after being used only for the internal budget
        # check.
        expected_cost = 3 * (500 / 1_000_000 * 3.00 + 100 / 1_000_000 * 15.00)
        assert result.cost_usd == pytest.approx(expected_cost)
        assert result.cost_usd > 0
        # Real wall-clock duration (this module's own time.monotonic()
        # measurement around the loop, since the anthropic SDK reports no
        # equivalent figure) — not a placeholder, not None.
        assert result.duration_ms is not None
        assert result.duration_ms >= 0

    def test_max_turns_exceeded_resolves_to_inconclusive(self, monkeypatch, conn):
        _patch_mcp(monkeypatch)
        # A backend that keeps calling a non-terminal tool forever.
        endless_response = CompletionResult(
            text="still working",
            tool_calls=[ToolCall(id="t", name="query_healthcare_db", input={"sql": "SELECT 1"})],
            usage={"input_tokens": 10, "output_tokens": 10},
        )
        backend = _ScriptedBackend(
            name="anthropic", supports_delegated_investigation=False, complete_responses=[endless_response] * 3
        )

        result = run_investigator(backend, _make_sentinel_finding(), conn, max_turns=3, max_budget_usd=5.0)

        assert result.finding.primary_root_cause == "inconclusive"
        assert "max_turns_exceeded" in result.finding.root_cause_summary
        assert result.finding.turns_used == 3
        assert result.finding.confidence == "low"

    def test_budget_exceeded_resolves_to_inconclusive(self, monkeypatch, conn):
        _patch_mcp(monkeypatch)
        # Deliberately huge token usage to blow past a tiny budget on turn 1.
        expensive_response = CompletionResult(
            text="thinking",
            tool_calls=[ToolCall(id="t", name="query_healthcare_db", input={"sql": "SELECT 1"})],
            usage={"input_tokens": 5_000_000, "output_tokens": 1_000_000},
        )
        backend = _ScriptedBackend(
            name="anthropic", supports_delegated_investigation=False, complete_responses=[expensive_response]
        )

        result = run_investigator(backend, _make_sentinel_finding(), conn, max_turns=12, max_budget_usd=0.01)

        assert result.finding.primary_root_cause == "inconclusive"
        assert "budget_exceeded" in result.finding.root_cause_summary
        assert result.finding.turns_used == 1
        # The whole reason this path fired: cost_usd genuinely exceeds
        # max_budget_usd. Confirms the inconclusive path still reports the
        # real running total, not None/0 just because the outcome wasn't a
        # clean success.
        assert result.cost_usd > 0.01

    def test_model_stops_without_submit_finding_resolves_to_inconclusive(self, monkeypatch, conn):
        _patch_mcp(monkeypatch)
        backend = _ScriptedBackend(
            name="anthropic",
            supports_delegated_investigation=False,
            complete_responses=[
                CompletionResult(text="I give up.", tool_calls=[], usage={"input_tokens": 10, "output_tokens": 10})
            ],
        )

        result = run_investigator(backend, _make_sentinel_finding(), conn, max_turns=12, max_budget_usd=5.0)

        assert result.finding.primary_root_cause == "inconclusive"
        assert "without calling submit_finding" in result.finding.root_cause_summary
        assert result.finding.turns_used == 1

    def test_mcp_server_unreachable_resolves_to_inconclusive_not_a_crash(self, monkeypatch, conn):
        def _raise_stdio_client(params):
            raise ConnectionRefusedError("mcp-server-datahub could not be started")

        monkeypatch.setattr(investigator, "stdio_client", _raise_stdio_client)
        backend = _ScriptedBackend(name="anthropic", supports_delegated_investigation=False, complete_responses=[])

        result = run_investigator(backend, _make_sentinel_finding(), conn, max_turns=12, max_budget_usd=5.0)

        assert result.finding.primary_root_cause == "inconclusive"
        assert "mcp_server_unreachable" in result.finding.root_cause_summary
        assert result.finding.turns_used is None
        # No turn ever ran (failed before the loop started) — cost_usd
        # should correctly read exactly 0.0, not None or a stale value.
        assert result.cost_usd == 0.0
        assert result.duration_ms is not None

    def test_llm_backend_error_mid_loop_resolves_to_inconclusive(self, monkeypatch, conn):
        _patch_mcp(monkeypatch)

        class _RaisingBackend:
            name = "anthropic"
            supports_delegated_investigation = False

            def complete(self, *a, **kw):
                raise BackendTimeoutError("timed out")

        result = run_investigator(_RaisingBackend(), _make_sentinel_finding(), conn, max_turns=12, max_budget_usd=5.0)

        assert result.finding.primary_root_cause == "inconclusive"
        assert "llm_backend_unavailable" in result.finding.root_cause_summary


class TestDesignADispatchHelpers:
    def test_dispatch_datahub_lineage_query_calls_mcp_session(self):
        session = _FakeClientSession()
        tool_call = ToolCall(id="t1", name="datahub_lineage_query", input={"mcp_tool_name": "search", "arguments": {"query": "claims"}})
        evidence = []

        result_text = asyncio.run(_dispatch_design_a_tool(tool_call, session, sqlite3.connect(":memory:"), None, evidence, turn=1))

        assert session.calls == [("search", {"query": "claims"})]
        assert "search" in result_text
        assert len(evidence) == 1
        assert evidence[0].tool == "datahub_lineage_query:search"

    def test_dispatch_query_healthcare_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (7)")
        tool_call = ToolCall(id="t1", name="query_healthcare_db", input={"sql": "SELECT * FROM t"})
        evidence = []

        result_text = asyncio.run(_dispatch_design_a_tool(tool_call, _FakeClientSession(), conn, None, evidence, turn=2))

        assert json.loads(result_text) == [{"x": 7}]
        assert evidence[0].tool == "query_healthcare_db"

    def test_dispatch_unknown_tool_returns_error_string(self):
        tool_call = ToolCall(id="t1", name="not_a_real_tool", input={})
        result_text = asyncio.run(
            _dispatch_design_a_tool(tool_call, _FakeClientSession(), sqlite3.connect(":memory:"), None, [], turn=1)
        )
        assert "unknown tool" in result_text

    def test_dispatch_mcp_call_failure_becomes_error_string_not_a_crash(self):
        class _RaisingSession:
            async def call_tool(self, name, arguments):
                raise RuntimeError("mcp server went away")

        tool_call = ToolCall(id="t1", name="datahub_lineage_query", input={"mcp_tool_name": "search", "arguments": {}})
        evidence = []
        result_text = asyncio.run(
            _dispatch_design_a_tool(tool_call, _RaisingSession(), sqlite3.connect(":memory:"), None, evidence, turn=1)
        )
        assert "ERROR" in result_text
        assert "mcp server went away" in result_text


class TestSmallPureHelpers:
    def test_estimate_cost_usd(self):
        cost = _estimate_cost_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
        assert cost == pytest.approx(3.00 + 15.00)

    def test_estimate_cost_usd_handles_missing_keys(self):
        assert _estimate_cost_usd({}) == 0.0

    def test_completion_to_content_blocks_text_only(self):
        blocks = _completion_to_content_blocks("hello", [])
        assert blocks == [{"type": "text", "text": "hello"}]

    def test_completion_to_content_blocks_with_tool_calls(self):
        blocks = _completion_to_content_blocks("checking", [ToolCall(id="t1", name="foo", input={"a": 1})])
        assert blocks == [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "t1", "name": "foo", "input": {"a": 1}},
        ]

    def test_completion_to_content_blocks_empty_text_omitted(self):
        blocks = _completion_to_content_blocks("", [ToolCall(id="t1", name="foo", input={})])
        assert blocks == [{"type": "tool_use", "id": "t1", "name": "foo", "input": {}}]


# ===========================================================================
# 4. Design B — ClaudeCodeBackend, delegated investigation.
# ===========================================================================


class TestExtractFencedJson:
    def test_single_block(self):
        text = 'Here is my finding:\n```json\n{"a": 1}\n```\nDone.'
        assert _extract_fenced_json(text) == {"a": 1}

    def test_uses_last_block_when_multiple_present(self):
        text = '```json\n{"a": "draft"}\n```\nActually, on reflection:\n```json\n{"a": "final"}\n```'
        assert _extract_fenced_json(text) == {"a": "final"}

    def test_no_block_raises_value_error(self):
        with pytest.raises(ValueError, match="no fenced"):
            _extract_fenced_json("I didn't produce any JSON at all.")

    def test_invalid_json_inside_block_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _extract_fenced_json("```json\n{not valid json,,,}\n```")


class TestDesignBInvestigate:
    def test_successful_parse(self, monkeypatch):
        finding_dict = _valid_finding_dict(root_cause="inherited_from:raw_patients")
        result_text = "Investigation complete.\n\n```json\n" + json.dumps(finding_dict) + "\n```"
        backend = _ScriptedBackend(
            name="claude_code",
            supports_delegated_investigation=True,
            investigate_result=InvestigationResult(
                result_text=result_text, is_error=False, cost_usd=0.42, duration_ms=9000, turns=6, raw={}
            ),
        )

        result = run_investigator(backend, _make_sentinel_finding(), sqlite3.connect(":memory:"))

        assert result.finding.primary_root_cause == "inherited_from:raw_patients"
        assert result.finding.turns_used == 6
        assert result.finding.backend_used == "claude_code"
        # The real DataHub-specific values this module owns were actually used.
        call = backend.investigate_calls[0]
        assert call["allowed_tools"] == DESIGN_B_ALLOWED_TOOLS
        assert call["mcp_config_path"] == investigator.MCP_CONFIG_PATH
        assert "Zorbex Insurance" in call["task_prompt"]
        # The cost-plumbing fix (Slice 4): Design B's real InvestigationResult
        # cost_usd/duration_ms (straight from claude -p's own JSON) now
        # actually reach the caller, instead of being extracted into
        # turns_used and dropped otherwise.
        assert result.cost_usd == 0.42
        assert result.duration_ms == 9000

    def test_malformed_json_resolves_to_inconclusive_not_a_crash(self):
        backend = _ScriptedBackend(
            name="claude_code",
            supports_delegated_investigation=True,
            investigate_result=InvestigationResult(
                result_text="I looked into it but ran out of things to say.", is_error=False, turns=3, raw={}
            ),
        )

        result = run_investigator(backend, _make_sentinel_finding(), sqlite3.connect(":memory:"))

        assert result.finding.primary_root_cause == "inconclusive"
        assert "could not parse" in result.finding.root_cause_summary
        assert "ran out of things to say" in result.finding.evidence[0].result_summary
        assert result.finding.turns_used == 3
        # The call itself completed (only the answer's shape was
        # unparseable) — cost/duration are real numbers here, not lost just
        # because the parse failed. This InvestigationResult didn't set
        # duration_ms explicitly (defaults to None), so this exercises the
        # documented fallback to this module's own wall-clock measurement.
        assert result.cost_usd is None  # InvestigationResult didn't set cost_usd either, in this canned response
        assert result.duration_ms is not None and result.duration_ms >= 0

    def test_budget_exhausted_resolves_to_inconclusive_with_subscription_limit_reason(self):
        backend = _ScriptedBackend(
            name="claude_code",
            supports_delegated_investigation=True,
            investigate_result=BudgetExhaustedError("exhausted --max-budget-usd cap"),
        )

        result = run_investigator(backend, _make_sentinel_finding(), sqlite3.connect(":memory:"))

        assert result.finding.primary_root_cause == "inconclusive"
        assert "subscription_limit" in result.finding.root_cause_summary
        assert result.finding.turns_used is None
        # Documented, deliberate gap (see _investigate_design_b's own
        # comment): BudgetExhaustedError carries the dollar figure only in
        # its message string, not as a structured attribute, so cost_usd
        # is honestly None here rather than a guess — but duration_ms is
        # still real (this module's own wall-clock measurement around the
        # failed call).
        assert result.cost_usd is None
        assert result.duration_ms is not None and result.duration_ms >= 0

    def test_other_llm_backend_error_resolves_to_inconclusive(self):
        backend = _ScriptedBackend(
            name="claude_code",
            supports_delegated_investigation=True,
            investigate_result=BackendCallError("claude -p returned non-JSON stdout"),
        )

        result = run_investigator(backend, _make_sentinel_finding(), sqlite3.connect(":memory:"))

        assert result.finding.primary_root_cause == "inconclusive"
        assert "llm_backend_unavailable" in result.finding.root_cause_summary

    def test_custom_overrides_are_passed_through(self):
        finding_dict = _valid_finding_dict()
        backend = _ScriptedBackend(
            name="claude_code",
            supports_delegated_investigation=True,
            investigate_result=InvestigationResult(
                result_text="```json\n" + json.dumps(finding_dict) + "\n```", turns=1, raw={}
            ),
        )
        custom_path = Path("/tmp/fake_mcp_config.json")

        run_investigator(
            backend,
            _make_sentinel_finding(),
            sqlite3.connect(":memory:"),
            mcp_config_path=custom_path,
            max_budget_usd=2.5,
            timeout_s=60.0,
        )

        call = backend.investigate_calls[0]
        assert call["mcp_config_path"] == custom_path
        assert call["max_budget_usd"] == 2.5
        assert call["timeout_s"] == 60.0


# ===========================================================================
# 5. run_investigator()'s top-level dispatch (decision 0005).
# ===========================================================================


class TestDispatch:
    def test_dispatches_to_design_b_when_supported(self):
        finding_dict = _valid_finding_dict()
        backend = _ScriptedBackend(
            name="claude_code",
            supports_delegated_investigation=True,
            investigate_result=InvestigationResult(
                result_text="```json\n" + json.dumps(finding_dict) + "\n```", turns=1, raw={}
            ),
        )
        run_investigator(backend, _make_sentinel_finding(), sqlite3.connect(":memory:"))
        assert len(backend.investigate_calls) == 1
        assert len(backend.complete_calls) == 0  # Design A's loop never ran

    def test_dispatches_to_design_a_when_not_supported(self, monkeypatch):
        _patch_mcp(monkeypatch)
        backend = _ScriptedBackend(
            name="anthropic",
            supports_delegated_investigation=False,
            complete_responses=[
                CompletionResult(
                    text="done", tool_calls=[ToolCall(id="t1", name="submit_finding", input=_valid_finding_dict())],
                    usage={"input_tokens": 10, "output_tokens": 10},
                )
            ],
        )
        run_investigator(backend, _make_sentinel_finding(), sqlite3.connect(":memory:"), max_budget_usd=5.0)
        assert len(backend.complete_calls) == 1
        assert len(backend.investigate_calls) == 0  # Design B never ran


# ===========================================================================
# 6. Live integration test — step 8. Skipped by default (pytest.ini).
# ===========================================================================


@pytest.mark.live
def test_live_investigation_against_real_data():
    """The one real, end-to-end integration test this slice makes (the
    second and last live `claude -p` call this slice makes overall — the
    first was step 1's --allowedTools confirmation, made before any of this
    module was written).

    Runs a REAL investigation, via ClaudeCodeBackend (the only backend
    actually usable in this environment — ANTHROPIC_API_KEY isn't
    configured, so Design A can't be live-tested here regardless of what
    this test picks), against the real local DataHub instance and the real
    committed healthcare.db, using a REAL SentinelFinding computed by
    actually calling run_sentinel() (Slice 1) — not a hand-built one.

    Segment chosen: UnitedHealthcare/diabetes (the ORIGINAL, direct-into-
    claims scenario) rather than Cigna/obesity. Either real seeded segment
    would prove the mechanism works; UnitedHealthcare/diabetes is chosen
    because its ground truth (90/10 split: introduced_at:claims for the
    majority, inherited_from:mart_billing for a real minority) is the
    HARDER, more discriminating case to get right — a model that just
    walks straight to raw_patients on reflex (over-eager "it must be
    upstream" bias) would get this one wrong in a way that's easy to check,
    whereas Cigna/obesity's clean 100%-upstream case is more forgiving of
    that same bias. Confirming this one is real evidence the hypothesis-
    testing instructions (§2.2, HYPOTHESIS_TESTING_INSTRUCTIONS) actually
    work, not just that the mechanism runs at all.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from agents.llm_backend import get_backend
    from agents.sentinel import run_sentinel

    db_path = Path(__file__).parent.parent / "src" / "datahub" / "healthcare.db"
    assert db_path.exists(), f"healthcare.db not found at {db_path}"

    conn = sqlite3.connect(str(db_path))
    findings = run_sentinel(conn, z_threshold=3.5)
    by_segment = {f.segment: f for f in findings}
    sentinel_finding = by_segment[Segment("UnitedHealthcare", "diabetes")]
    assert sentinel_finding.flagged is True

    backend = get_backend("claude_code")
    # max_budget_usd overridden above the .env default (0.75): step 1's own
    # cost data (a single tool call alone cost ~$0.19-0.22) shows the
    # default is tight for a real multi-tool, multi-turn investigation.
    # Set generously here, before running, specifically so this one live
    # shot doesn't fail on a premature budget cutoff the way step 1's FIRST
    # attempt did — not a post-hoc adjustment after seeing this test fail.
    result = run_investigator(backend, sentinel_finding, conn, max_budget_usd=2.0)
    conn.close()

    print("\n=== LIVE Investigator result (UnitedHealthcare/diabetes) ===")
    print(json.dumps(result.finding.to_dict(), indent=2, default=str))
    # Slice 4's cost-plumbing fix: real cost/duration now surface here too
    # (this test itself is what originally exposed the gap in Slice 3's own
    # report — result.cost_usd/duration_ms didn't exist at all when this
    # test was first written and run).
    print(f"cost_usd: {result.cost_usd}")
    print(f"duration_ms: {result.duration_ms}")

    assert result.finding.primary_root_cause != "inconclusive", (
        f"expected a confident root cause, got inconclusive: {result.finding.root_cause_summary}"
    )
    # The evidence-backed ground truth for this specific seeded scenario
    # (lld-sprint2.md §0/§10): the anomaly is introduced AT claims, not
    # inherited from any upstream table.
    assert result.finding.primary_root_cause == "introduced_at:claims", (
        f"expected introduced_at:claims, got {result.finding.primary_root_cause}: {result.finding.root_cause_summary}"
    )
