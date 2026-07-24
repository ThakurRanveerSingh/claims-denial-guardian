#!/usr/bin/env python3
"""
Investigator — root-cause tracing for a Sentinel-flagged anomaly.
Design: docs/architecture/lld-sprint2.md §2; the Design A/B split is decision
0004; the explicit `supports_delegated_investigation` dispatch is decision
0005; the two real seeded scenarios this workflow has to actually get right
are docs/architecture/lld-sprint2.md §10 / decision 0006 (Slice 0).

What this module does, in one sentence: given a `SentinelFinding` (Slice 1's
output — a flagged segment) and an `LLMBackend` (Slice 2), run an
LLM-driven investigation that confirms schema live, walks lineage, breaks
denials down by reason code, and tests "does this anomaly reproduce at each
upstream hop?" one hop at a time — then returns a structured
`InvestigatorFinding` (§2.3) with a quantified root cause.

Two structurally different mechanisms, dispatched on
`backend.supports_delegated_investigation` (decision 0005), never guessed at:

  - Design B (`ClaudeCodeBackend` only): delegate the WHOLE investigation to
    one `backend.investigate()` call (Slice 2's already-generic mechanism —
    this module supplies the real DataHub-specific values Slice 2 explicitly
    deferred: `investigator_mcp_config.json`, the real `mcp__datahub__*`
    tool names, the pinned read-only `sqlite3` Bash command, and the actual
    task prompt). The model does its own multi-turn tool loop internally;
    this module's job is building the right prompt and parsing the fenced
    JSON block out of its final answer (decision 0004's named "known
    accepted risk" — no structured-output guarantee for this path).
  - Design A (`AnthropicBackend`/`OllamaBackend`): THIS module owns the
    entire turn-by-turn loop (decision 0005: `llm_backend.py` stays generic,
    with zero DataHub/healthcare.db knowledge — that domain knowledge lives
    here). Three tools per §2.5: a DataHub MCP relay (`datahub_lineage_query`,
    spawning `mcp-server-datahub` directly via the `mcp` Python SDK — the
    same product pattern decision 0003 establishes, a separate connection
    from Claude Code's own `.mcp.json`), a read-only `healthcare.db` query
    tool (`query_healthcare_db`), and a terminal `submit_finding` tool whose
    input schema IS `InvestigatorFinding` (Anthropic's tool-use API
    guarantees the arguments match that schema — no parsing needed for this
    path, unlike Design B).

The hypothesis-testing workflow itself (§2.2 — confirm schema live, walk
lineage, break down by denial_reason_code, test reproduction hop-by-hop,
quantify rather than classify, report affected_branch as exactly what the
evidence implicates) is genuinely LLM-driven in both designs: the model
writes its own SQL and picks its own DataHub MCP calls. This module does NOT
hardcode either scenario's answer — `HYPOTHESIS_TESTING_INSTRUCTIONS` below
states the general procedure once, worded identically in spirit for both
designs (a system-prompt-style instruction for Design A, a structured task
prompt for Design B), and the model is expected to arrive at
`introduced_at:claims` (90/10 split) for `UnitedHealthcare`/`diabetes` and
`inherited_from:raw_patients` (100%) for `Cigna`/`obesity` by actually
running the checks against the real data (Slice 0's two seeded scenarios),
not because either segment name appears anywhere in this file.
"""

import asyncio
import json
import os
import re
import sqlite3
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agents.llm_backend import (
    BudgetExhaustedError,
    LLMBackend,
    LLMBackendError,
    ToolCall,
)
from agents.sentinel import SentinelFinding

# Same load_dotenv() convention as every other module in this repo.
load_dotenv()

_MODULE_DIR = Path(__file__).resolve().parent

# The real, committed database (decision 0002) — resolved to an ABSOLUTE
# path deliberately, not the relative "src/datahub/healthcare.db" shown
# illustratively in lld-sprint2.md §2.6. An absolute path here removes a
# real fragility the LLD's example didn't need to deal with: Design B's
# pinned Bash tool string (DESIGN_B_ALLOWED_TOOLS, below) has to match
# EXACTLY what the model actually runs, and llm_backend.py's
# `_run_claude()` (Slice 2, not modified this slice) does not set an
# explicit subprocess `cwd=` — so a relative path's correctness would
# depend on the calling process's current working directory, an assumption
# this module doesn't need to make when an absolute path works identically
# well and removes the dependency entirely.
DB_PATH = (_MODULE_DIR.parent / "datahub" / "healthcare.db").resolve()

# Committed alongside this module (Step 1 of this slice's own pre-
# implementation checklist, lld-sprint2.md §10.9) — same shape as the
# repo-root .mcp.json, ${VAR}-expansion, no literal secrets.
MCP_CONFIG_PATH = _MODULE_DIR / "investigator_mcp_config.json"

# The DataHub MCP server's confirmed read-only tool surface (lld-sprint2.md
# §0/§2.5) — re-confirmed live this slice (§10.9's gate) with one real
# `claude -p --allowedTools "mcp__datahub__search"` call before any of this
# module was written: the model reached and used the tool correctly,
# returning `urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.claims,PROD)`
# with real owner/container metadata. That confirms the `mcp__datahub__<tool>`
# --allowedTools string format works exactly as §2.6 assumed.
DATAHUB_MCP_TOOLS = [
    "search",
    "get_entities",
    "get_lineage",
    "get_lineage_paths_between",
    "list_schema_fields",
    "get_dataset_queries",
    "grep_documents",
    "search_documents",
]

# Design B's pinned, read-only Bash command shape (§2.6) — enforced at the
# sqlite3 CLI level (-readonly), the same enforcement principle as Design
# A's read-only connection string (query_healthcare_db, below), just
# expressed as a CLI flag instead of a connection-string parameter.
DESIGN_B_ALLOWED_TOOLS = [f"mcp__datahub__{tool}" for tool in DATAHUB_MCP_TOOLS] + [
    f"Bash(sqlite3 -readonly {DB_PATH} *)"
]

# --- Implementation-time defaults, named explicitly and .env-overridable —
# same convention as src/agents/sentinel.py's SENTINEL_Z_THRESHOLD. ---
DEFAULT_MAX_TURNS = 12  # LLD §5: estimated 6-7 turns for the happy path, 12 gives headroom
DEFAULT_MAX_BUDGET_USD = 0.75  # LLD §7: "a starting default around $0.50-$1.00... not a precisely derived number"


def _default_max_turns() -> int:
    return int(os.environ.get("INVESTIGATOR_MAX_TURNS", str(DEFAULT_MAX_TURNS)))


def _default_max_budget_usd() -> float:
    return float(os.environ.get("INVESTIGATOR_MAX_BUDGET_USD", str(DEFAULT_MAX_BUDGET_USD)))


def _default_investigate_timeout_s() -> float:
    # Reuses Slice 2's own default (a wall-clock backstop, §5) rather than
    # inventing a second one — DEFAULT_INVESTIGATE_TIMEOUT_S is imported
    # lazily inside run_investigator() to avoid a module-load-order
    # dependency on llm_backend.py beyond what's already imported above.
    from agents.llm_backend import DEFAULT_INVESTIGATE_TIMEOUT_S

    return DEFAULT_INVESTIGATE_TIMEOUT_S


# ---------------------------------------------------------------------------
# InvestigatorFinding — §2.3's output contract, implemented for real.
# ---------------------------------------------------------------------------


@dataclass
class RootCauseBreakdownEntry:
    """One entry in InvestigatorFinding.root_cause_breakdown — §2.3: "full
    accounting... two entries for this sprint's seeded case: 325/90.0%
    introduced_at:claims, 36/10.0% inherited_from:mart_billing"."""

    classification: str
    claim_count: int
    pct: float
    note: str


@dataclass
class EvidenceEntry:
    """One entry in InvestigatorFinding.evidence — "every check actually
    made, in order... what makes an inconclusive result useful to a human
    picking up where Investigator stopped" (§2.3)."""

    step: str
    tool: str
    query_or_call: str
    result_summary: str


@dataclass
class InvestigatorFinding:
    """§2.3's contract, all ten fields, implemented for real (the LLD left
    this illustrative — this is the first slice that needs an actual object
    both Design A's tool schema and Design B's parsed JSON can produce)."""

    primary_root_cause: str  # "introduced_at:<dataset>" | "inherited_from:<dataset>" | "inconclusive"
    root_cause_breakdown: list[RootCauseBreakdownEntry]
    affected_branch: list[str]
    datasets_checked_and_clean: list[str]
    lineage_path_walked: list[str]
    evidence: list[EvidenceEntry]
    root_cause_summary: str
    confidence: str  # "high" | "medium" | "low"
    backend_used: str  # "claude_code" | "anthropic" | "ollama"
    turns_used: Optional[int]  # populated for Design A; may be None for Design B (§2.3)

    def to_dict(self) -> dict:
        """Plain-dict form — Slice 4's Orchestrator will eventually
        serialize this into examples/<incident-id>/incident.json (§4.2);
        this method exists now so that shape doesn't need inventing twice."""
        return asdict(self)


@dataclass
class InvestigatorRunResult:
    """run_investigator()'s REAL return shape — added in Slice 4 to close a
    gap Slice 4's own report named: `InvestigatorFinding` deliberately has
    no cost/duration fields (§2.3's ten fields are the finding's substance;
    operational metadata is `Incident.cost`'s job, §4.2, owned by
    Orchestrator) — but nothing was plumbing that data OUT of
    run_investigator() at all. Design B's real `InvestigationResult.cost_usd`
    was being extracted into `turns_used` and silently dropped otherwise;
    Design A's `_estimate_cost_usd()` accumulated a running total used only
    internally for the budget check, never returned. Fixed here, once, for
    both designs, since `InvestigatorFinding`'s own already-committed ten-
    field shape (§2.3) is NOT reopened by this fix — this wraps it instead.

    `finding`: unchanged, exactly §2.3's contract.
    `cost_usd`: real dollars for Design B (`InvestigationResult.cost_usd`,
      straight from `claude -p`'s own JSON) when available; the running
      `_estimate_cost_usd()` total for Design A (a labeled ESTIMATE, per that
      function's own docstring, since the raw `anthropic` SDK response has
      no dollar figure at all); `None` only for Design B's exception paths
      where no `InvestigationResult` was ever returned at all (see
      `_investigate_design_b`'s comment on exactly why).
    `duration_ms`: real wall-clock milliseconds either way — Design B prefers
      `InvestigationResult.duration_ms` (reported directly by `claude -p`)
      when available, falling back to this module's own `time.monotonic()`
      measurement around the call otherwise; Design A always uses its own
      `time.monotonic()` measurement around the whole loop, since the
      `anthropic` SDK doesn't report an equivalent figure itself.
    """

    finding: InvestigatorFinding
    cost_usd: Optional[float]
    duration_ms: Optional[float]


# The exact JSON schema InvestigatorFinding's model-facing contract must
# match — used TWICE, deliberately, so the two designs can't silently drift
# apart: as Design A's `submit_finding` tool's `input_schema` (Anthropic
# enforces this at the API level), and rendered into Design B's task prompt
# as the shape its final fenced JSON block must match (unenforced there —
# decision 0004's named risk — but at least both designs are being asked
# for the identical shape, not two hand-maintained near-duplicates).
SUBMIT_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "primary_root_cause": {
            "type": "string",
            "description": (
                "One of: 'introduced_at:<dataset>' (the anomaly first appears at this "
                "dataset; upstream sources are clean), 'inherited_from:<dataset>' (the "
                "anomaly already exists at this upstream dataset), or 'inconclusive' "
                "(the evidence does not cleanly settle it)."
            ),
        },
        "root_cause_breakdown": {
            "type": "array",
            "description": "Full accounting of EVERY classification found among the flagged claims, not just the majority one.",
            "items": {
                "type": "object",
                "properties": {
                    "classification": {"type": "string"},
                    "claim_count": {"type": "integer"},
                    "pct": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["classification", "claim_count", "pct", "note"],
            },
        },
        "affected_branch": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Dataset names that actually need remediation — exactly what the evidence implicates, not automatically the full lineage chain.",
        },
        "datasets_checked_and_clean": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Dataset names you checked and found clean — named explicitly, not just omitted.",
        },
        "lineage_path_walked": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What get_lineage / get_lineage_paths_between actually returned, live.",
        },
        "evidence": {
            "type": "array",
            "description": "Every check you actually made, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "tool": {"type": "string"},
                    "query_or_call": {"type": "string"},
                    "result_summary": {"type": "string"},
                },
                "required": ["step", "tool", "query_or_call", "result_summary"],
            },
        },
        "root_cause_summary": {
            "type": "string",
            "description": "Plain-language narrative citing the evidence, for a human reader.",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": [
        "primary_root_cause",
        "root_cause_breakdown",
        "affected_branch",
        "datasets_checked_and_clean",
        "lineage_path_walked",
        "evidence",
        "root_cause_summary",
        "confidence",
    ],
}


def _breakdown_from_dicts(items: Any) -> list[RootCauseBreakdownEntry]:
    """Lenient by design (`.get()` with defaults, not `**item`): Design A's
    tool-call arguments are schema-guaranteed by Anthropic's API, but Design
    B's are free-form JSON the model wrote inside a text block — a slightly
    off shape there (a missing key, say) should degrade gracefully into a
    usable-if-imperfect entry, not blow up the whole parse over one field.
    """
    return [
        RootCauseBreakdownEntry(
            classification=str(item.get("classification", "")),
            claim_count=int(item.get("claim_count", 0)),
            pct=float(item.get("pct", 0.0)),
            note=str(item.get("note", "")),
        )
        for item in items
    ]


def _evidence_from_dicts(items: Any) -> list[EvidenceEntry]:
    return [
        EvidenceEntry(
            step=str(item.get("step", "")),
            tool=str(item.get("tool", "")),
            query_or_call=str(item.get("query_or_call", "")),
            result_summary=str(item.get("result_summary", "")),
        )
        for item in items
    ]


def finding_from_model_output(data: dict, *, backend_used: str, turns_used: Optional[int]) -> InvestigatorFinding:
    """Build an InvestigatorFinding from a model-produced dict — Design A's
    `submit_finding` tool-call arguments, or Design B's parsed fenced JSON
    block. Both arrive in the same shape (SUBMIT_FINDING_SCHEMA), so one
    function builds the real dataclass from either.

    `primary_root_cause` is the one field read with `data["..."]` (not
    `.get()`) deliberately — a finding with no root cause at all isn't a
    finding, and the KeyError this raises for a missing/malformed model
    response is exactly what the caller's try/except turns into an honest
    "inconclusive" result (§6 failure mode 5), rather than silently
    fabricating a default root cause that was never actually concluded.
    """
    return InvestigatorFinding(
        primary_root_cause=data["primary_root_cause"],
        root_cause_breakdown=_breakdown_from_dicts(data.get("root_cause_breakdown", [])),
        affected_branch=[str(x) for x in data.get("affected_branch", [])],
        datasets_checked_and_clean=[str(x) for x in data.get("datasets_checked_and_clean", [])],
        lineage_path_walked=[str(x) for x in data.get("lineage_path_walked", [])],
        evidence=_evidence_from_dicts(data.get("evidence", [])),
        root_cause_summary=str(data.get("root_cause_summary", "")),
        confidence=str(data.get("confidence", "low")),
        backend_used=backend_used,
        turns_used=turns_used,
    )


def _inconclusive_finding(
    *, reason: str, evidence: list[EvidenceEntry], backend_used: str, turns_used: Optional[int]
) -> InvestigatorFinding:
    """§6's shared shape for every named failure mode (1: MCP unreachable, 3:
    budget/quota hit, 5: evidence doesn't settle) — an honest incomplete
    answer, not a forced-confident guess, with whatever evidence was
    actually gathered before the failure preserved rather than discarded.
    """
    return InvestigatorFinding(
        primary_root_cause="inconclusive",
        root_cause_breakdown=[],
        affected_branch=[],
        datasets_checked_and_clean=[],
        lineage_path_walked=[],
        evidence=evidence,
        root_cause_summary=reason,
        confidence="low",
        backend_used=backend_used,
        turns_used=turns_used,
    )


# ---------------------------------------------------------------------------
# Shared prompt content — the hypothesis-testing workflow itself (§2.2),
# worded once and reused by both designs so they can't silently drift apart
# on what "the investigation" actually means.
# ---------------------------------------------------------------------------


def _describe_sentinel_finding(finding: SentinelFinding) -> str:
    provider, condition = finding.segment
    return (
        f"Segment: {provider} / {condition}\n"
        f"Claims: {finding.segment_claim_count}, Denied: {finding.segment_denial_count} "
        f"({finding.segment_denial_rate:.2%})\n"
        f"Baseline denial rate (leave-one-out, every other segment): {finding.baseline_denial_rate:.2%}\n"
        f"z-score: {finding.z_score:.2f} (threshold {finding.threshold})\n"
        f"Sentinel's summary: {finding.summary}"
    )


# Worded as a general procedure — no segment name, no expected answer baked
# in anywhere. Both of Slice 0's real seeded scenarios (and any future one)
# are meant to be reachable by actually following these steps against
# whatever the live data shows, not by this text hinting at an answer.
HYPOTHESIS_TESTING_INSTRUCTIONS = textwrap.dedent(
    """
    Investigation steps (follow in order; skip a step only if it's genuinely
    not applicable, and say so):

    1. Confirm the ACTUAL current schema of `claims`, `denials`, and any
       upstream tables you plan to query — live, before writing any SQL
       against them. Never assume a column name without checking first.
    2. Walk lineage upstream from `claims` to see the actual registered
       chain, live — don't assume a fixed shape from prior knowledge; read it.
    3. Break the flagged segment's denials down by `denial_reason_code`
       before picking a hypothesis. Only `INVALID_BILLING_AMOUNT` maps to a
       known, testable data-quality hypothesis in this schema
       (`billing_amount < 0`) — `RANDOM_AUDIT` / `HIGH_RISK_SCORE` denials
       have no underlying field-level defect to trace by design. If some
       other reason code dominates instead, say plainly that there is no
       known data-quality hypothesis to test for it here, rather than
       forcing this hypothesis onto data it doesn't apply to.
    4. For the INVALID_BILLING_AMOUNT-denied claims specifically, test
       "does this reproduce at the immediate upstream source?" one hop at a
       time, starting nearest. The immediate upstream join is
       `claims.source_billing_rowid = mart_billing.rowid`. Stop at the
       FIRST hop where the anomaly (billing_amount < 0) stops reproducing
       for (near-)all flagged rows. If it DOES reproduce at a hop, continue
       one hop further (mart_billing -> staging_patients -> raw_patients)
       and repeat — do not assume "this is upstream" after checking only
       one hop; keep walking as far as the evidence actually reproduces.
    5. Quantify, don't just classify: report exactly how many of the
       flagged claims each explanation accounts for. If part of the flagged
       set is explained one way and a different, smaller part is explained
       another way (e.g. most reproduce at one hop, a smaller remainder
       reflects an unrelated pre-existing baseline defect), report BOTH as
       separate entries in root_cause_breakdown — do not blend two
       different explanations into one number.
    6. Report `affected_branch` as exactly what the evidence implicates —
       this may be a single dataset, or the full chain back to
       raw_patients, depending on what the reproduction checks actually
       showed. List every other dataset you checked and found clean in
       `datasets_checked_and_clean`.
    7. If the evidence doesn't cleanly settle (a reproduction rate lands
       neither near 0% nor near 100%, or a check fails/is blocked), report
       `primary_root_cause` as "inconclusive" with `confidence: "low"` and
       every step you actually took preserved in `evidence` — an honest
       incomplete answer beats a forced-confident guess.
    """
).strip()


# ---------------------------------------------------------------------------
# Design B — ClaudeCodeBackend, delegates the whole investigation.
# ---------------------------------------------------------------------------


_FENCED_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_fenced_json(text: str) -> dict:
    """Pull the LAST fenced ```json``` block out of the model's final
    answer. LAST, not first: if the model second-guesses itself mid-answer
    and an earlier draft block appears before its real conclusion, the last
    one is the one actually intended as the final answer. Raises ValueError
    (caller catches this and produces an "inconclusive" result, §6 failure
    mode 5) if no fenced block is found at all — decision 0004's named
    accepted risk for this path, made concrete here rather than left
    abstract.
    """
    matches = _FENCED_JSON_RE.findall(text or "")
    if not matches:
        raise ValueError("no fenced ```json``` block found in the model's final answer")
    return json.loads(matches[-1])


def _design_b_task_prompt(finding: SentinelFinding) -> str:
    # Built with an explicit list of already-left-aligned lines, joined at
    # the end, rather than one big textwrap.dedent(f\"\"\"...\"\"\") — dedent()
    # looks for common leading whitespace across EVERY line in the string,
    # including interpolated multi-line content
    # (_describe_sentinel_finding()'s output, HYPOTHESIS_TESTING_INSTRUCTIONS,
    # the json.dumps() block) whose own lines start at column 0. Mixing
    # those into one indented template collapses dedent()'s common-prefix
    # detection and leaves half the text still indented — a real, if purely
    # cosmetic, bug caught by actually printing this prompt during
    # implementation rather than trusting it unread.
    lines = [
        "You are Investigator, an agent that traces the root cause of a claims-denial",
        "anomaly that a statistical detector (Sentinel) has already flagged.",
        "",
        "Sentinel's finding:",
        _describe_sentinel_finding(finding),
        "",
        HYPOTHESIS_TESTING_INSTRUCTIONS,
        "",
        "Tools available to you:",
        f"- DataHub MCP tools ({', '.join(DATAHUB_MCP_TOOLS)}) for live schema and",
        "  lineage confirmation.",
        "- Read-only SQL against the actual data: run",
        f'  `sqlite3 -readonly {DB_PATH} "<SELECT ...>"` as a Bash command, exactly',
        "  in that form. Only SELECT statements will do anything useful — the",
        "  connection itself is opened read-only, so nothing else can succeed.",
        "",
        "When you are done, your FINAL answer must end with a single fenced JSON",
        "code block (```json ... ```) matching EXACTLY this shape (every field is",
        "required):",
        "",
        json.dumps(SUBMIT_FINDING_SCHEMA, indent=2),
        "",
        "Do not include any other fenced JSON code block anywhere in your answer —",
        "only the one final block, matching the schema above, as the last thing",
        "you write.",
    ]
    return "\n".join(lines)


def _investigate_design_b(
    backend: LLMBackend,
    finding: SentinelFinding,
    *,
    mcp_config_path: Path,
    max_budget_usd: float,
    timeout_s: float,
) -> InvestigatorRunResult:
    task_prompt = _design_b_task_prompt(finding)
    call_started = time.monotonic()

    try:
        result = backend.investigate(
            task_prompt=task_prompt,
            mcp_config_path=mcp_config_path,
            allowed_tools=DESIGN_B_ALLOWED_TOOLS,
            max_budget_usd=max_budget_usd,
            timeout_s=timeout_s,
        )
    except BudgetExhaustedError as e:
        # §6 failure mode 3's specific, distinguishable reason string — the
        # fix (wait, or switch LLM_BACKEND) is different from real ambiguity,
        # and must not be conflated with it in the record.
        #
        # cost_usd=None here, deliberately, not a guess: BudgetExhaustedError
        # (Slice 2, llm_backend.py — not touched by this fix) carries the
        # dollar figure only inside its message string ("cost so far: ..."),
        # not as a structured attribute, and widening that exception's shape
        # is out of scope for this narrow fix. The figure is still visible
        # to a human reading root_cause_summary below (it's interpolated
        # into `e`) — just not available as a structured
        # Incident.cost.investigator_cost_usd number for this one failure
        # path. Named explicitly rather than silently returning 0.0 (which
        # would be actively wrong — real budget WAS spent) or fabricating a
        # number.
        return InvestigatorRunResult(
            finding=_inconclusive_finding(
                reason=f"llm_backend_unavailable: subscription_limit ({e})",
                evidence=[
                    EvidenceEntry(
                        step="delegate investigation",
                        tool="claude -p (Design B)",
                        query_or_call=f"investigate(mcp_config_path={mcp_config_path})",
                        result_summary=f"budget exhausted before completion: {e}",
                    )
                ],
                backend_used=backend.name,
                turns_used=None,
            ),
            cost_usd=None,
            duration_ms=(time.monotonic() - call_started) * 1000,
        )
    except LLMBackendError as e:
        # §6 failure mode 1 (MCP unreachable) and any other llm_backend.py
        # failure (CLI missing, timeout, malformed response) — all real,
        # named failures that must resolve to inconclusive, not a crash.
        return InvestigatorRunResult(
            finding=_inconclusive_finding(
                reason=f"llm_backend_unavailable: {e}",
                evidence=[
                    EvidenceEntry(
                        step="delegate investigation",
                        tool="claude -p (Design B)",
                        query_or_call=f"investigate(mcp_config_path={mcp_config_path})",
                        result_summary=f"backend call failed: {e}",
                    )
                ],
                backend_used=backend.name,
                turns_used=None,
            ),
            cost_usd=None,
            duration_ms=(time.monotonic() - call_started) * 1000,
        )

    # Prefer claude -p's own reported duration (real, from the CLI's own
    # JSON) over this module's wall-clock measurement, which also includes
    # Python-side overhead (subprocess spawn/teardown, JSON parsing) the CLI's
    # own figure doesn't. Fall back to the wall-clock measurement only if the
    # CLI didn't report one for some reason.
    measured_duration_ms = (time.monotonic() - call_started) * 1000
    duration_ms = result.duration_ms if result.duration_ms is not None else measured_duration_ms

    try:
        data = _extract_fenced_json(result.result_text)
        finding_out = finding_from_model_output(data, backend_used=backend.name, turns_used=result.turns)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        # Decision 0004's named "known accepted risk": Design B has no
        # structured-output guarantee. A malformed/missing JSON block is a
        # real (if rare) failure mode — handled as inconclusive with the raw
        # text preserved as evidence, not a crash. cost_usd/duration_ms ARE
        # available here (unlike the exception paths above) — the call
        # itself completed, only the answer's shape was unparseable, so
        # there's no reason to lose real cost data over a parsing problem.
        return InvestigatorRunResult(
            finding=_inconclusive_finding(
                reason=f"could not parse InvestigatorFinding from the model's final answer: {e}",
                evidence=[
                    EvidenceEntry(
                        step="parse final answer",
                        tool="claude -p (Design B)",
                        query_or_call="extract fenced ```json``` block from result_text",
                        result_summary=(result.result_text or "")[:2000],
                    )
                ],
                backend_used=backend.name,
                turns_used=result.turns,
            ),
            cost_usd=result.cost_usd,
            duration_ms=duration_ms,
        )
    return InvestigatorRunResult(finding=finding_out, cost_usd=result.cost_usd, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Design A — AnthropicBackend/OllamaBackend, this module's own turn loop.
# ---------------------------------------------------------------------------

# Best-effort, NOT authoritative: unlike Design B (whose `claude -p` CLI JSON
# reports total_cost_usd directly), the raw `anthropic` SDK response has no
# dollar-cost field at all — just token counts. These per-million-token
# rates are an approximation for tracking INVESTIGATOR_MAX_BUDGET_USD in
# Design A's loop, not a fact the SDK reported. If Anthropic's real pricing
# has moved by the time this runs, this estimate will be off by that delta —
# named explicitly (not buried) so it's obvious this is an assumption. LLD
# §7 calls this out as real implementation work for whoever builds the
# agent loop; this is that work, done honestly rather than skipped.
ESTIMATED_INPUT_COST_PER_MTOK = 3.00
ESTIMATED_OUTPUT_COST_PER_MTOK = 15.00


def _estimate_cost_usd(usage: dict) -> float:
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    return (input_tokens / 1_000_000) * ESTIMATED_INPUT_COST_PER_MTOK + (
        output_tokens / 1_000_000
    ) * ESTIMATED_OUTPUT_COST_PER_MTOK


def _design_a_system_prompt(finding: SentinelFinding) -> str:
    # Same "explicit list of pre-aligned lines" approach as
    # _design_b_task_prompt(), and for the identical reason — see that
    # function's comment.
    lines = [
        "You are Investigator, an agent that traces the root cause of a claims-denial",
        "anomaly that a statistical detector (Sentinel) has already flagged, using",
        "live DataHub lineage lookups and direct read-only SQL against healthcare.db.",
        "",
        "Sentinel's finding:",
        _describe_sentinel_finding(finding),
        "",
        HYPOTHESIS_TESTING_INSTRUCTIONS,
        "",
        "Use the datahub_lineage_query and query_healthcare_db tools to gather",
        "evidence for each step above. When you have enough evidence to reach a",
        "conclusion — or to honestly conclude the evidence doesn't settle it — call",
        "submit_finding exactly once with your complete finding. Do not just describe",
        "your conclusion in text; you must call submit_finding to end the",
        "investigation.",
    ]
    return "\n".join(lines)


DATAHUB_LINEAGE_QUERY_TOOL = {
    "name": "datahub_lineage_query",
    "description": (
        "Query the DataHub MCP server for live schema, lineage, or metadata. "
        "Pick which underlying DataHub MCP tool to call and supply its "
        "arguments; the raw result is returned as-is. Valid mcp_tool_name "
        "values: " + ", ".join(DATAHUB_MCP_TOOLS) + "."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mcp_tool_name": {"type": "string", "enum": DATAHUB_MCP_TOOLS},
            "arguments": {
                "type": "object",
                "description": "Arguments for the chosen DataHub MCP tool, matching that tool's own parameter shape.",
            },
        },
        "required": ["mcp_tool_name", "arguments"],
    },
}

QUERY_HEALTHCARE_DB_TOOL = {
    "name": "query_healthcare_db",
    "description": (
        "Run a single read-only SELECT statement against healthcare.db and "
        "return the resulting rows as JSON. Only SELECT is allowed, enforced "
        "two ways: a keyword check, and the underlying connection itself "
        "being opened read-only — so no write can succeed even if the SQL "
        "tried. Results are capped at 500 rows."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A SELECT-only SQL statement."}},
        "required": ["sql"],
    },
}

SUBMIT_FINDING_TOOL = {
    "name": "submit_finding",
    "description": "Submit your final, complete InvestigatorFinding. Calling this ends the investigation.",
    "input_schema": SUBMIT_FINDING_SCHEMA,
}

DESIGN_A_TOOLS = [DATAHUB_LINEAGE_QUERY_TOOL, QUERY_HEALTHCARE_DB_TOOL, SUBMIT_FINDING_TOOL]


_SELECT_ONLY_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


def _resolve_db_path(conn: sqlite3.Connection) -> Optional[Path]:
    """Find the real on-disk file `conn` points at, so query_healthcare_db
    can open its OWN dedicated read-only connection to that exact database
    (§2.5's real enforcement layer) instead of trusting however the caller
    happened to open `conn`. Returns None for an in-memory (":memory:")
    connection — there is no second file-backed handle to reopen read-only
    in that case (sqlite3's own well-known behavior: each ":memory:"
    connection is a wholly separate, isolated database by construction; a
    read-only "copy" of one doesn't exist to open). Real runs always use a
    file-backed DB_PATH; the in-memory fallback below exists for unit tests
    only, and is documented there as providing weaker (keyword-only)
    enforcement for that specific case.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    file_path = row[2] if row else ""
    return Path(file_path) if file_path else None


def _query_healthcare_db(sql: str, conn: sqlite3.Connection, db_path: Optional[Path]) -> str:
    if not _SELECT_ONLY_RE.match((sql or "").strip()):
        return "ERROR: only SELECT statements are allowed by query_healthcare_db."

    try:
        if db_path is not None:
            # The real enforcement layer (§2.5): even if the keyword check
            # above had a gap, this connection is physically incapable of
            # writing to the file — matters because healthcare.db is a
            # committed, tracked file (decision 0002).
            ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cursor = ro_conn.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                rows = cursor.fetchmany(500)  # cap what reaches the model's context
            finally:
                ro_conn.close()
        else:
            # In-memory DB (unit tests only — see _resolve_db_path's
            # docstring). The keyword check above is the only enforcement
            # available in this branch; real runs never take it.
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(500)
    except sqlite3.Error as e:
        return f"ERROR: {e}"

    return json.dumps([dict(zip(columns, row)) for row in rows], default=str)


async def _call_mcp_tool(session: ClientSession, mcp_tool_name: str, arguments: dict) -> str:
    result = await session.call_tool(mcp_tool_name, arguments)
    # CallToolResult.content is a list of content blocks (TextContent for
    # every DataHub MCP tool confirmed this session, per §10.9's live
    # check) — flatten to plain text for the model.
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts)


def _completion_to_content_blocks(text: str, tool_calls: list[ToolCall]) -> list[dict]:
    """Reconstruct Anthropic Messages API content blocks from the ABSTRACTED
    CompletionResult fields (text/tool_calls), not from `completion.raw`
    directly. Deliberate: decision 0004 frames Design A as portable across
    backends ("same loop, same tool schemas, just a different complete()
    underneath... works for Anthropic and (eventually) Ollama") — reading
    `.raw` here would silently assume an Anthropic-shaped response object,
    breaking that portability the moment a second real Design-A backend
    exists. The trade-off, stated honestly: this drops any content-block
    types beyond text/tool_use (e.g. extended-thinking blocks), which
    aren't part of this project's scope this sprint.
    """
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in tool_calls:
        blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
    return blocks


async def _dispatch_design_a_tool(
    tool_call: ToolCall,
    session: ClientSession,
    conn: sqlite3.Connection,
    db_path: Optional[Path],
    evidence: list[EvidenceEntry],
    turn: int,
) -> str:
    if tool_call.name == "datahub_lineage_query":
        mcp_tool_name = tool_call.input.get("mcp_tool_name", "")
        arguments = tool_call.input.get("arguments", {}) or {}
        try:
            result_text = await _call_mcp_tool(session, mcp_tool_name, arguments)
        except Exception as e:  # noqa: BLE001 — any MCP call failure becomes tool-result text, not a crash
            result_text = f"ERROR calling DataHub MCP tool {mcp_tool_name!r}: {e}"
        evidence.append(
            EvidenceEntry(
                step=f"turn {turn}",
                tool=f"datahub_lineage_query:{mcp_tool_name}",
                query_or_call=json.dumps(arguments, default=str),
                result_summary=result_text[:1000],
            )
        )
        return result_text

    if tool_call.name == "query_healthcare_db":
        sql = tool_call.input.get("sql", "")
        result_text = _query_healthcare_db(sql, conn, db_path)
        evidence.append(
            EvidenceEntry(step=f"turn {turn}", tool="query_healthcare_db", query_or_call=sql, result_summary=result_text[:1000])
        )
        return result_text

    return f"ERROR: unknown tool {tool_call.name!r}"


async def _investigate_design_a_async(
    backend: LLMBackend,
    finding: SentinelFinding,
    conn: sqlite3.Connection,
    *,
    max_turns: int,
    max_budget_usd: float,
) -> InvestigatorRunResult:
    db_path = _resolve_db_path(conn)
    evidence: list[EvidenceEntry] = []
    cumulative_cost_usd = 0.0
    loop_started = time.monotonic()

    def _elapsed_ms() -> float:
        # The `anthropic` SDK reports no per-call latency figure the way
        # claude -p's own JSON does (§0) — this module's own wall-clock
        # measurement, around the WHOLE loop, is the only real duration
        # figure Design A has to report. Called at every return point below
        # so every outcome (success, every inconclusive path, every
        # exception) gets a real number, not just the happy path.
        return (time.monotonic() - loop_started) * 1000

    server_params = StdioServerParameters(
        command="uvx",
        args=["mcp-server-datahub@latest"],
        env={
            **os.environ,
            "DATAHUB_GMS_URL": os.environ.get("DATAHUB_GMS_URL", ""),
            "DATAHUB_GMS_TOKEN": os.environ.get("DATAHUB_GMS_TOKEN", ""),
        },
    )

    messages: list[dict] = [{"role": "user", "content": _design_a_system_prompt(finding)}]

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                for turn in range(1, max_turns + 1):
                    completion = backend.complete(messages, tools=DESIGN_A_TOOLS, max_tokens=4096)
                    cumulative_cost_usd += _estimate_cost_usd(completion.usage)

                    messages.append(
                        {"role": "assistant", "content": _completion_to_content_blocks(completion.text, completion.tool_calls)}
                    )

                    if cumulative_cost_usd > max_budget_usd:
                        evidence.append(
                            EvidenceEntry(
                                step=f"turn {turn}",
                                tool="budget check",
                                query_or_call=f"cumulative estimated cost ${cumulative_cost_usd:.4f}",
                                result_summary=f"exceeded INVESTIGATOR_MAX_BUDGET_USD=${max_budget_usd:.2f}",
                            )
                        )
                        return InvestigatorRunResult(
                            finding=_inconclusive_finding(
                                reason=(
                                    f"llm_backend_unavailable: budget_exceeded "
                                    f"(estimated ${cumulative_cost_usd:.4f} > ${max_budget_usd:.2f})"
                                ),
                                evidence=evidence,
                                backend_used=backend.name,
                                turns_used=turn,
                            ),
                            cost_usd=cumulative_cost_usd,
                            duration_ms=_elapsed_ms(),
                        )

                    submit_call = next((tc for tc in completion.tool_calls if tc.name == "submit_finding"), None)
                    if submit_call is not None:
                        return InvestigatorRunResult(
                            finding=finding_from_model_output(submit_call.input, backend_used=backend.name, turns_used=turn),
                            cost_usd=cumulative_cost_usd,
                            duration_ms=_elapsed_ms(),
                        )

                    if not completion.tool_calls:
                        # The model stopped without calling submit_finding —
                        # not a crash, but not a confident structured
                        # finding either (§2.2 step 7's spirit, applied to
                        # "the model just... stopped" rather than only to
                        # ambiguous evidence).
                        evidence.append(
                            EvidenceEntry(
                                step=f"turn {turn}",
                                tool="model response",
                                query_or_call="(no tool call)",
                                result_summary=(completion.text or "")[:2000],
                            )
                        )
                        return InvestigatorRunResult(
                            finding=_inconclusive_finding(
                                reason="model ended the conversation without calling submit_finding",
                                evidence=evidence,
                                backend_used=backend.name,
                                turns_used=turn,
                            ),
                            cost_usd=cumulative_cost_usd,
                            duration_ms=_elapsed_ms(),
                        )

                    tool_result_blocks = []
                    for tool_call in completion.tool_calls:
                        result_text = await _dispatch_design_a_tool(tool_call, session, conn, db_path, evidence, turn)
                        tool_result_blocks.append(
                            {"type": "tool_result", "tool_use_id": tool_call.id, "content": result_text}
                        )
                    messages.append({"role": "user", "content": tool_result_blocks})

                # MAX_TURNS exhausted without submit_finding — LLD §5's
                # primary guardrail for this design, tripped.
                return InvestigatorRunResult(
                    finding=_inconclusive_finding(
                        reason=f"llm_backend_unavailable: max_turns_exceeded (INVESTIGATOR_MAX_TURNS={max_turns})",
                        evidence=evidence,
                        backend_used=backend.name,
                        turns_used=max_turns,
                    ),
                    cost_usd=cumulative_cost_usd,
                    duration_ms=_elapsed_ms(),
                )
    except LLMBackendError as e:
        return InvestigatorRunResult(
            finding=_inconclusive_finding(
                reason=f"llm_backend_unavailable: {e}", evidence=evidence, backend_used=backend.name, turns_used=None
            ),
            cost_usd=cumulative_cost_usd,
            duration_ms=_elapsed_ms(),
        )
    except Exception as e:  # noqa: BLE001 — deliberately broad: §6 failure mode 1
        # (MCP server unreachable/fails to start) and any other unexpected
        # failure in the async transport/loop resolve to inconclusive here,
        # not a crash that kills the whole run. Trade-off, stated honestly:
        # this also catches genuine bugs in this loop's own code as
        # "inconclusive" rather than surfacing them as a traceback — an
        # accepted cost given the LLD's explicit preference for graceful
        # degradation over crashes in this component, and given this loop
        # is otherwise thoroughly exercised by mocked unit tests that WOULD
        # surface such a bug directly (they call the un-caught pieces).
        evidence.append(
            EvidenceEntry(
                step="connect to DataHub MCP server / run investigation loop",
                tool="mcp-server-datahub (stdio)",
                query_or_call="uvx mcp-server-datahub@latest",
                result_summary=f"unreachable or failed: {e}",
            )
        )
        return InvestigatorRunResult(
            finding=_inconclusive_finding(
                reason=f"mcp_server_unreachable_or_loop_error: {e}",
                evidence=evidence,
                backend_used=backend.name,
                turns_used=None,
            ),
            cost_usd=cumulative_cost_usd,
            duration_ms=_elapsed_ms(),
        )


def _investigate_design_a(
    backend: LLMBackend, finding: SentinelFinding, conn: sqlite3.Connection, *, max_turns: int, max_budget_usd: float
) -> InvestigatorRunResult:
    return asyncio.run(
        _investigate_design_a_async(backend, finding, conn, max_turns=max_turns, max_budget_usd=max_budget_usd)
    )


# ---------------------------------------------------------------------------
# Top-level entry point — decision 0005's dispatch.
# ---------------------------------------------------------------------------


def run_investigator(
    backend: LLMBackend,
    finding: SentinelFinding,
    conn: sqlite3.Connection,
    *,
    mcp_config_path: Optional[Path] = None,
    max_budget_usd: Optional[float] = None,
    max_turns: Optional[int] = None,
    timeout_s: Optional[float] = None,
) -> InvestigatorRunResult:
    """Investigate a Sentinel-flagged segment, returning an
    InvestigatorRunResult (finding + real cost/duration — see that
    dataclass's own docstring for why this isn't a bare InvestigatorFinding)
    regardless of which design actually ran (decision 0005's dispatch, made
    here, once):

        if backend.supports_delegated_investigation:
            Design B — backend.investigate(), Slice 2's generic mechanism,
            supplied with the real DataHub-specific values this module owns.
        else:
            Design A — this module's own turn-by-turn loop.

    Named `run_investigator`, not `investigate`, deliberately: matches
    src/agents/sentinel.py's `run_sentinel()` naming convention for a
    module's top-level entry point. `LLMBackend.investigate()` is a
    different, lower-level method on a different object — already
    distinguishable by the object it's called on, so sharing the verb
    doesn't create real ambiguity, and consistency with the sibling module's
    naming is worth more here than mirroring `LLMBackend`'s own method name.

    `conn`: the connection Sentinel's own finding was computed against (or
    an equivalent one). Design A's query_healthcare_db tool derives the
    real on-disk file path from THIS connection (_resolve_db_path) and opens
    its own dedicated read-only connection to that exact file for actually
    running the model's SQL — real enforcement, not just trusting `conn`
    itself was opened read-only. Design B doesn't use `conn` directly at
    all (it runs `sqlite3 -readonly <DB_PATH>` as a separate subprocess) —
    accepted here anyway so both designs share one call signature.

    Callers who only want the finding itself (e.g. reading
    `.primary_root_cause`) use `run_investigator(...).finding` — a one-
    attribute-deeper access is the real, honest cost of exposing cost/
    duration at all, preferred here over silently attaching non-§2.3 fields
    onto `InvestigatorFinding` itself (which decision 0005/§2.3 already
    settled as a fixed ten-field contract — not reopened by this fix).
    """
    if max_budget_usd is None:
        max_budget_usd = _default_max_budget_usd()

    if backend.supports_delegated_investigation:
        return _investigate_design_b(
            backend,
            finding,
            mcp_config_path=mcp_config_path or MCP_CONFIG_PATH,
            max_budget_usd=max_budget_usd,
            timeout_s=timeout_s if timeout_s is not None else _default_investigate_timeout_s(),
        )

    return _investigate_design_a(
        backend,
        finding,
        conn,
        max_turns=max_turns if max_turns is not None else _default_max_turns(),
        max_budget_usd=max_budget_usd,
    )
