#!/usr/bin/env python3
"""
Drift — a single, on-demand feature-health check for `denial_risk_model`
(US-6). Design: docs/architecture/lld-sprint3-wp4.md, honesty verdict in
that file's §5, decision 0010 (regeneration non-determinism).

Not "drift over time" — this project's synthetic, single-generation
dataset has no genuine second time-point to compare against (verified
empirically, §5 of the LLD: three independent pieces of evidence, not
assumed). What this module actually checks: whether the CURRENT feature
data still satisfies its own documented mathematical properties —
`segment_denial_rate`'s [0,1] range invariant, `billing_zscore`'s
documented cap (`BILLING_ZSCORE_CAP` in `score_claims.py`), and its shape
against the theoretical standard normal it's supposed to approximate.
Single snapshot, zero LLM, no scheduling — one on-demand check per
`guardian check-drift` invocation, exactly as scoped in the LLD's §4.

Two entry points, split by concern the same way Scribe/Reporter already
are in this codebase:
  - `run_drift_check()` — pure read: feature list from the live
    `denial_risk_features` MLFeatureTable (via MCP, per decision 0003 —
    never hardcoded), checks computed against the local `healthcare.db`.
    No DataHub write.
  - `run_drift_writeback(finding)` — extends Scribe's exact tag/doc-note/
    assertion pattern (decision 0007) onto the `denial_risk_model` MLModel
    entity, same idempotency discipline, same "SDK writes, MCP reads"
    split (decision 0003).

`MODEL_VERSION`/`BILLING_ZSCORE_CAP` are duplicated here from
`src/datahub/score_claims.py`, not imported — `src/datahub/` deliberately
has no `__init__.py` (pyproject.toml's own comment: avoiding a name
collision with the real `datahub` SDK package), so it isn't an importable
package. Same "small per-module boilerplate, copied not shared"
convention `reporter.py`'s module docstring already states for this
codebase.
"""

import asyncio
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Callable, Optional

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    AssertionInfoClass,
    AssertionResultClass,
    AssertionRunEventClass,
    AssertionSourceClass,
    AuditStampClass,
    CustomAssertionInfoClass,
    GlobalTagsClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
    TagAssociationClass,
    TagPropertiesClass,
)
from datahub.metadata.urns import MlFeatureTableUrn, MlModelUrn

load_dotenv()

DATAHUB_SERVER = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
DATAHUB_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

_MODULE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _MODULE_DIR.parent.parent
DB_PATH = (_REPO_ROOT / "src" / "datahub" / "healthcare.db").resolve()

# ---------------------------------------------------------------------------
# Constants — matching src/datahub/register_ml_model.py's own URN
# construction (same SDK URN classes, not hand-built strings) and
# src/datahub/score_claims.py's scoring constants (duplicated, see module
# docstring for why).
# ---------------------------------------------------------------------------

ML_PLATFORM = "urn:li:dataPlatform:python"
FEATURE_TABLE_URN = str(MlFeatureTableUrn(platform=ML_PLATFORM, name="denial_risk_features"))
MODEL_URN = str(MlModelUrn(platform=ML_PLATFORM, name="denial_risk_model", env="PROD"))

MODEL_VERSION = "toy-denial-risk-v1"  # score_claims.py's MODEL_VERSION
BILLING_ZSCORE_CAP = 4.0  # score_claims.py's BILLING_ZSCORE_CAP

# PSI convention (Population Stability Index) — a widely-published
# threshold, not invented for this project (LLD §2.2): <0.1 no
# significant shift, 0.1-0.25 moderate, >0.25 significant.
PSI_FLAG_THRESHOLD = 0.10
PSI_BINS = 10

DRIFT_TAG_URN = "urn:li:tag:denial_guardian_drift_checked"
DRIFT_TAG_NAME = "denial_guardian_drift_checked"
DRIFT_TAG_DESCRIPTION = (
    "denial_risk_model has had at least one Guardian feature-health check "
    "run against it. See the entity's documentation notes "
    "(institutionalMemory) for the individual check results — this tag "
    "does not by itself mean the most recent check passed."
)


def _assertion_urn_for_feature(feature_name: str) -> str:
    """One assertion per (model, feature) pair, reused across every check
    run — same reuse-not-duplicate discipline as Scribe's
    `_assertion_urn_for()` (decision 0007)."""
    return f"urn:li:assertion:denial_guardian_{feature_name}_health"


# ---------------------------------------------------------------------------
# Result types.
# ---------------------------------------------------------------------------


@dataclass
class FeatureHealthCheck:
    feature_name: str  # "segment_denial_rate" | "billing_zscore" | ...
    check_type: str  # "range_invariant" | "cap_exceedance" | "shape_vs_theoretical" | "unimplemented"
    documented_expected: str  # e.g. "[0.0, 1.0]", "|z| <= 4.0 (BILLING_ZSCORE_CAP)", "PSI < 0.10 vs N(0,1)"
    metric_value: float  # the computed number (exceedance %, PSI, out-of-range count)
    status: str  # "pass" | "flagged"
    plain_summary: str  # one sentence, no jargon — for the audit report narrative


@dataclass
class DriftFinding:
    check_id: str  # f"drift-{utc_timestamp}", keys writeback idempotency
    model_version: str
    checked_at: str  # ISO 8601 UTC
    feature_checks: list = field(default_factory=list)  # list[FeatureHealthCheck]
    overall_status: str = "pass"  # "pass" | "N check(s) flagged"


@dataclass
class FeatureAssertionResult:
    feature_name: str
    assertion_urn: str
    assertion_defined: bool = False
    assertion_already_defined: bool = False
    assertion_run_event_emitted: bool = False


@dataclass
class DriftWritebackResult:
    check_id: str
    entity_urn: Optional[str] = None
    tag_applied: bool = False
    tag_already_present: bool = False
    doc_note_added: bool = False
    doc_note_already_present: bool = False
    feature_assertions: list = field(default_factory=list)  # list[FeatureAssertionResult]
    skipped_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# The checks themselves — zero LLM, closed-form arithmetic against
# denial_model_scores (score_claims.py's own output table). LLD §2.
# ---------------------------------------------------------------------------


def _psi_vs_standard_normal(values: list, n_bins: int = PSI_BINS) -> float:
    """Population Stability Index of `values`'s shape against a theoretical
    N(0,1) — NOT a comparison against past data (none exists, LLD §5); a
    single-snapshot check of whether billing_zscore's distribution still
    resembles what the z-scoring transform is supposed to approximate.
    Z-scoring fixes mean/variance by construction (see the rejected
    mean/std check below) but not shape, so this is genuinely informative,
    not tautological."""
    if not values:
        return 0.0
    n = len(values)
    nd = NormalDist(0, 1)
    cut_points = [nd.inv_cdf(i / n_bins) for i in range(1, n_bins)]
    edges = [-math.inf] + cut_points + [math.inf]
    counts = [0] * n_bins
    for v in values:
        for i in range(n_bins):
            if edges[i] <= v < edges[i + 1]:
                counts[i] += 1
                break
    expected = 1.0 / n_bins
    psi = 0.0
    for c in counts:
        observed = max(c / n, 1e-6)  # avoid log(0) for an empty bin
        psi += (observed - expected) * math.log(observed / expected)
    return psi


def _check_segment_denial_rate_range(conn: sqlite3.Connection) -> list:
    """Range invariant, not a drift signal (LLD §2.2): `denial_rate =
    denied_count / len(rows)` is mathematically guaranteed to be in [0,1]
    by construction of score_claims.py's own arithmetic. This always
    passes while the pipeline's math is intact — it's a corruption/
    regression guard, the same category as Scribe's existing
    billing-amount assertion, not evidence of distributional change."""
    row = conn.execute(
        "SELECT MIN(segment_denial_rate_feature), MAX(segment_denial_rate_feature) FROM denial_model_scores"
    ).fetchone()
    lo, hi = (row[0], row[1]) if row and row[0] is not None else (0.0, 0.0)
    out_of_range = conn.execute(
        "SELECT COUNT(*) FROM denial_model_scores "
        "WHERE segment_denial_rate_feature < 0.0 OR segment_denial_rate_feature > 1.0"
    ).fetchone()[0]

    if out_of_range == 0:
        status = "pass"
        summary = (
            f"segment_denial_rate stayed within its mathematically valid 0-100% range for every "
            f"scored claim (observed range: {lo:.1%} to {hi:.1%}). This is a data-integrity check, "
            f"not a distributional comparison — it always passes when the underlying calculation is "
            f"working correctly, and would only fail if that calculation itself were broken."
        )
    else:
        status = "flagged"
        summary = (
            f"{out_of_range} scored claim(s) have a segment_denial_rate outside the valid 0-100% "
            f"range (observed range: {lo:.1%} to {hi:.1%}) — this points to a bug in the underlying "
            f"calculation or corrupted data, not distributional drift."
        )
    return [
        FeatureHealthCheck(
            feature_name="segment_denial_rate",
            check_type="range_invariant",
            documented_expected="[0.0, 1.0]",
            metric_value=float(out_of_range),
            status=status,
            plain_summary=summary,
        )
    ]


def _check_billing_zscore_health(conn: sqlite3.Connection) -> list:
    """Two checks, NOT a mean/std shift check. A naive mean/std check on
    billing_zscore is rejected (LLD §2.1) as tautological: z-scoring
    within each segment guarantees pooled mean=0/std=1 by construction,
    regardless of the underlying data's real health — confirmed both
    algebraically and against the live data before this was written.
    What's checked instead: (1) exceedance of the model's own documented
    cap (BILLING_ZSCORE_CAP), and (2) PSI-based shape comparison against
    the theoretical standard normal — both genuinely informative,
    single-snapshot, non-circular checks."""
    values = [row[0] for row in conn.execute("SELECT billing_zscore_feature FROM denial_model_scores").fetchall()]
    n = len(values)

    exceed_count = sum(1 for v in values if abs(v) > BILLING_ZSCORE_CAP)
    exceed_pct = (exceed_count / n * 100) if n else 0.0
    # No hard flag threshold for cap exceedance in v1 — the LLD explicitly
    # rejected inventing one; reported plainly for visibility rather than
    # flagged against a made-up number (LLD §2.2).
    cap_check = FeatureHealthCheck(
        feature_name="billing_zscore",
        check_type="cap_exceedance",
        documented_expected=f"|z| <= {BILLING_ZSCORE_CAP} (BILLING_ZSCORE_CAP, score_claims.py)",
        metric_value=exceed_pct,
        status="pass",
        plain_summary=(
            f"{exceed_count} of {n} scored claims ({exceed_pct:.2f}%) have a billing_zscore beyond "
            f"the model's own documented boundary of {BILLING_ZSCORE_CAP}. Reported for visibility "
            f"only — this version does not flag against any threshold here, to avoid treating an "
            f"invented number as a meaningful cutoff."
        ),
    )

    psi = _psi_vs_standard_normal(values)
    psi_status = "pass" if psi < PSI_FLAG_THRESHOLD else "flagged"
    shape_check = FeatureHealthCheck(
        feature_name="billing_zscore",
        check_type="shape_vs_theoretical",
        documented_expected=f"PSI < {PSI_FLAG_THRESHOLD} vs. theoretical standard normal (published PSI convention)",
        metric_value=psi,
        status=psi_status,
        plain_summary=(
            f"billing_zscore's observed shape has a Population Stability Index of {psi:.4f} against "
            f"the theoretical standard normal distribution it's mathematically supposed to "
            f"approximate — {'within the healthy range' if psi_status == 'pass' else 'outside the healthy range'} "
            f"under the standard PSI convention (under 0.10 means no significant shift). This "
            f"compares shape against a mathematical reference, not against a past snapshot of this "
            f"data — no such historical snapshot genuinely exists for this project's dataset."
        ),
    )
    return [cap_check, shape_check]


FEATURE_CHECKS: dict = {
    "segment_denial_rate": _check_segment_denial_rate_range,
    "billing_zscore": _check_billing_zscore_health,
}


# ---------------------------------------------------------------------------
# MCP reads — same session-spawning pattern scribe.py already uses
# (duplicated deliberately, not extracted — decision 0007's Consequences
# section already states why for this codebase).
# ---------------------------------------------------------------------------


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command="uvx",
        args=["mcp-server-datahub@latest"],
        env={**os.environ, "DATAHUB_GMS_URL": DATAHUB_SERVER, "DATAHUB_GMS_TOKEN": DATAHUB_TOKEN or ""},
    )


async def _call_mcp_tool(session: ClientSession, tool_name: str, arguments: dict) -> dict:
    import json

    result = await session.call_tool(tool_name, arguments)
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    raw = "\n".join(parts)
    return json.loads(raw)


_FEATURE_URN_RE = re.compile(r"^urn:li:mlFeature:\([^,]+,([^)]+)\)$")


async def _fetch_feature_names(session: ClientSession) -> list:
    """The set of features to check comes from DataHub, not a Python
    literal (LLD §3.1, per point 1's explicit "don't hardcode" ask) — the
    CHECK IMPLEMENTATIONS still have to live in code (FEATURE_CHECKS
    above), the same way Reporter's severity_for() thresholds are
    necessarily code; DataHub can say a feature named X exists, not what a
    sane range for X is."""
    data = await _call_mcp_tool(session, "get_entities", {"urns": FEATURE_TABLE_URN})
    features = ((data.get("featureTableProperties") or {}).get("mlFeatures")) or []
    names = []
    for f in features:
        m = _FEATURE_URN_RE.match(f.get("urn", ""))
        if m:
            names.append(m.group(1))
    return names


async def _assertion_already_exists(session: ClientSession, assertion_urn: str) -> bool:
    """Identical logic to scribe.py's own `_assertion_already_exists` —
    duplicated per this codebase's "small per-module boilerplate, copied
    not shared" convention, not imported across modules."""
    try:
        data = await _call_mcp_tool(session, "get_entities", {"urns": assertion_urn})
    except Exception:
        return False
    return "info" in data


def _current_tag_urns(details: dict) -> set:
    tags = (details.get("tags") or {}).get("tags") or []
    return {t["tag"]["urn"] for t in tags if "tag" in t and "urn" in t["tag"]}


def _read_institutional_memory(graph, urn: str) -> dict:
    """SDK/GraphQL read, not MCP — same verified exception scribe.py's own
    `_read_institutional_memory` documents (the MCP server's
    `relatedDocuments` field is a different, unrelated feature)."""
    result = graph.execute_graphql(
        "query($urn: String!) { mlModel(urn: $urn) { institutionalMemory { elements { url description created { time } } } } }",
        variables={"urn": urn},
    )
    return (result.get("mlModel") or {}).get("institutionalMemory") or {}


_DOC_ID_RE = re.compile(r"^\[(drift-[^\]]+)\]")


def _parse_drift_doc_entries(institutional_memory: dict):
    """Which check_ids already have a doc note on the model entity, and
    the full existing element list (institutionalMemory is a whole-list
    aspect — decision 0007). Same shape as scribe.py's
    `_parse_doc_entries`, keyed by `check_id` ("drift-...") instead of
    `incident_id` ("INC-...")."""
    ids: set = set()
    elements = []
    for entry in institutional_memory.get("elements", []) or []:
        desc = entry.get("description", "") or ""
        m = _DOC_ID_RE.match(desc)
        if m:
            ids.add(m.group(1))
        created = entry.get("created") or {}
        elements.append(
            InstitutionalMemoryMetadataClass(
                url=entry.get("url", ""),
                description=desc,
                createStamp=AuditStampClass(
                    time=created.get("time", 0) if isinstance(created, dict) else 0,
                    actor="urn:li:corpuser:datahub",
                ),
            )
        )
    return ids, elements


# ---------------------------------------------------------------------------
# SDK writes (decision 0003: writes via the REST emitter, not MCP).
# ---------------------------------------------------------------------------


def _ensure_drift_tag_exists(emitter: DatahubRestEmitter) -> None:
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=DRIFT_TAG_URN,
            aspect=TagPropertiesClass(name=DRIFT_TAG_NAME, description=DRIFT_TAG_DESCRIPTION),
        )
    )


def _apply_drift_tag(emitter: DatahubRestEmitter, entity_urn: str, current_tags: set) -> bool:
    if DRIFT_TAG_URN in current_tags:
        return False
    new_tags = current_tags | {DRIFT_TAG_URN}
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=entity_urn,
            aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=t) for t in sorted(new_tags)]),
        )
    )
    return True


def _build_drift_doc_description(finding: DriftFinding) -> str:
    """"[<check_id>] " prefix is the dedup key `_parse_drift_doc_entries()`
    parses back out — not just cosmetic, same convention as Scribe's
    `_build_doc_description`."""
    parts = [f"{c.feature_name}/{c.check_type}: {c.status}" for c in finding.feature_checks]
    return (
        f"[{finding.check_id}] Guardian feature-health check — model_version {finding.model_version}, "
        f"{finding.overall_status}. {'; '.join(parts)}. Single-snapshot internal-consistency check, "
        f"not a comparison against historical data (see docs/decisions/0010)."
    )


def _append_drift_doc_note(emitter: DatahubRestEmitter, entity_urn: str, existing_elements: list, finding: DriftFinding) -> bool:
    now_ms = int(time.time() * 1000)
    new_entry = InstitutionalMemoryMetadataClass(
        url=f"urn:li:corpuser:guardian#{finding.check_id}",
        description=_build_drift_doc_description(finding),
        createStamp=AuditStampClass(time=now_ms, actor="urn:li:corpuser:datahub"),
    )
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=entity_urn,
            aspect=InstitutionalMemoryClass(elements=existing_elements + [new_entry]),
        )
    )
    return True


def _ensure_drift_assertion_defined(emitter: DatahubRestEmitter, assertion_urn: str, model_urn: str, feature_name: str, documented_expected: str) -> None:
    info = AssertionInfoClass(
        type="CUSTOM",
        customAssertion=CustomAssertionInfoClass(
            type="FEATURE_HEALTH_CHECK",
            entity=model_urn,
            field=feature_name,
        ),
        source=AssertionSourceClass(type="INFERRED"),
        description=(
            f"Guardian: {feature_name} internal-consistency check against its documented expected "
            f"range ({documented_expected}). Single-snapshot, not drift-over-time — see "
            f"docs/decisions/0010-regeneration-non-determinism.md."
        ),
    )
    emitter.emit(MetadataChangeProposalWrapper(entityUrn=assertion_urn, aspect=info))


def _emit_drift_assertion_run_event(
    emitter: DatahubRestEmitter, assertion_urn: str, model_urn: str, check_id: str, checked_at: str, flagged_count: int,
) -> None:
    """Same (assertionUrn, timestampMillis, runId) dedup mechanic Scribe's
    own assertion run events already rely on (decision 0007, empirically
    verified there) — runId=check_id, timestampMillis derived from the
    check's own checked_at, both deterministic."""
    dt = datetime.fromisoformat(checked_at)
    timestamp_millis = int(dt.timestamp() * 1000)
    run_event = AssertionRunEventClass(
        timestampMillis=timestamp_millis,
        runId=check_id,
        asserteeUrn=model_urn,
        status="COMPLETE",
        assertionUrn=assertion_urn,
        result=AssertionResultClass(
            type="FAILURE" if flagged_count else "SUCCESS",
            unexpectedCount=flagged_count,
        ),
    )
    emitter.emit(MetadataChangeProposalWrapper(entityUrn=assertion_urn, aspect=run_event))


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


async def _run_drift_check_async(healthcare_db_path: Path) -> DriftFinding:
    check_id = f"drift-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    checked_at = datetime.now(timezone.utc).isoformat()

    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            feature_names = await _fetch_feature_names(session)

    conn = sqlite3.connect(f"file:{healthcare_db_path}?mode=ro", uri=True)
    try:
        checks: list = []
        for name in feature_names:
            check_fn: Optional[Callable] = FEATURE_CHECKS.get(name)
            if check_fn is None:
                # Quarantine-not-hide (decision 0008's discipline, applied
                # here too): DataHub names a feature this module has no
                # check for -- say so explicitly rather than silently
                # skip it.
                checks.append(
                    FeatureHealthCheck(
                        feature_name=name,
                        check_type="unimplemented",
                        documented_expected="(none defined)",
                        metric_value=0.0,
                        status="flagged",
                        plain_summary=(
                            f"DataHub's denial_risk_features table lists '{name}' as a model feature, "
                            f"but no health check is implemented for it yet."
                        ),
                    )
                )
                continue
            checks.extend(check_fn(conn))
    finally:
        conn.close()

    flagged = [c for c in checks if c.status == "flagged"]
    overall_status = "pass" if not flagged else f"{len(flagged)} check(s) flagged"

    return DriftFinding(
        check_id=check_id, model_version=MODEL_VERSION, checked_at=checked_at,
        feature_checks=checks, overall_status=overall_status,
    )


def run_drift_check(healthcare_db_path: Optional[Path] = None) -> DriftFinding:
    """Top-level entry point, matching sentinel.py/scribe.py's run_*()
    naming convention. Pure read — no DataHub write; see
    run_drift_writeback() for the write side."""
    if healthcare_db_path is None:
        healthcare_db_path = DB_PATH
    return asyncio.run(_run_drift_check_async(healthcare_db_path))


def _unique_feature_names(finding: DriftFinding) -> list:
    seen = []
    for c in finding.feature_checks:
        if c.feature_name not in seen:
            seen.append(c.feature_name)
    return seen


async def _run_drift_writeback_async(finding: DriftFinding) -> DriftWritebackResult:
    result = DriftWritebackResult(check_id=finding.check_id)

    emitter = DatahubRestEmitter(DATAHUB_SERVER, token=DATAHUB_TOKEN)
    graph = DataHubGraph(DatahubClientConfig(server=DATAHUB_SERVER, token=DATAHUB_TOKEN))
    _ensure_drift_tag_exists(emitter)

    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            try:
                details = await _call_mcp_tool(session, "get_entities", {"urns": MODEL_URN})
            except Exception:
                # A genuinely nonexistent entity raises (ItemNotFoundError
                # from the MCP server), it doesn't round-trip as an empty
                # dict — confirmed live before writing this, not assumed;
                # unlike _assertion_already_exists, where "doesn't exist
                # yet" is the ordinary, expected first-run case, this is
                # the true "shouldn't happen" path (the model is already
                # registered by src/datahub/register_ml_model.py), so it's
                # reported rather than silently swallowed.
                result.skipped_reason = "denial_risk_model entity not found in DataHub"
                return result
            result.entity_urn = MODEL_URN

            # --- 1. Tag ---
            current_tags = _current_tag_urns(details)
            if DRIFT_TAG_URN in current_tags:
                result.tag_already_present = True
            else:
                result.tag_applied = _apply_drift_tag(emitter, MODEL_URN, current_tags)

            # --- 2. Doc note ---
            institutional_memory = _read_institutional_memory(graph, MODEL_URN)
            existing_ids, existing_elements = _parse_drift_doc_entries(institutional_memory)
            if finding.check_id in existing_ids:
                result.doc_note_already_present = True
            else:
                result.doc_note_added = _append_drift_doc_note(emitter, MODEL_URN, existing_elements, finding)

            # --- 3. Assertions, one per feature (not per individual check --
            # LLD §3.3: cap-exceedance + shape checks on billing_zscore
            # share one assertion) ---
            for feature_name in _unique_feature_names(finding):
                assertion_urn = _assertion_urn_for_feature(feature_name)
                feature_checks = [c for c in finding.feature_checks if c.feature_name == feature_name]
                ar = FeatureAssertionResult(feature_name=feature_name, assertion_urn=assertion_urn)

                if await _assertion_already_exists(session, assertion_urn):
                    ar.assertion_already_defined = True
                else:
                    documented = "; ".join(c.documented_expected for c in feature_checks)
                    _ensure_drift_assertion_defined(emitter, assertion_urn, MODEL_URN, feature_name, documented)
                    ar.assertion_defined = True

                flagged_count = sum(1 for c in feature_checks if c.status == "flagged")
                _emit_drift_assertion_run_event(emitter, assertion_urn, MODEL_URN, finding.check_id, finding.checked_at, flagged_count)
                ar.assertion_run_event_emitted = True
                result.feature_assertions.append(ar)

    return result


def run_drift_writeback(finding: DriftFinding) -> DriftWritebackResult:
    """Top-level entry point for the write side — extends Scribe's exact
    tag/doc-note/assertion pattern (decision 0007) onto the
    denial_risk_model MLModel entity, same idempotency discipline."""
    return asyncio.run(_run_drift_writeback_async(finding))
