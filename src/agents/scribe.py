#!/usr/bin/env python3
"""
Scribe — writes what Investigator found back into DataHub (US-4).
Design: docs/decisions/0007-scribe-writeback-design.md. Read that file for
the full reasoning (including two live-verified gotchas: the schemaField
URN parent-URN requirement, and institutionalMemory's whole-list-overwrite
risk) — this module implements it, not re-derives it.

Three writes, only on entities named in InvestigatorFinding.affected_branch
(never datasets_checked_and_clean — that's US-2's "selective halting" story
carried through to the writeback, not re-decided here):

  1. A single generic `guardian-incident` tag (not one per incident — see
     decision 0007 for why a tag-per-incident was rejected).
  2. A documentation note appended to institutionalMemory, linking to the
     incident's own committed examples/<id>/incident.json on GitHub.
  3. An assertion (`billing_amount >= 0`, this sprint's one concrete,
     verified expectation — see decision 0007's scope section) plus a run
     event recording this specific incident's violation, but ONLY on
     entities that actually have a billing_amount column (checked live via
     MCP schema read, never assumed).

Per decision 0003: reads go through the DataHub MCP server
(mcp-server-datahub, spawned the same way investigator.py's Design A loop
already does — a separate connection from Claude Code's own dev-time
.mcp.json). Writes go through the acryl-datahub SDK's REST emitter with
DATAHUB_GMS_TOKEN, the same mechanism every src/datahub/ setup script and
Investigator's Design A tool dispatch already use.

ONE verified, documented exception: reading back institutionalMemory
(_read_institutional_memory) uses the SDK/GraphQL directly, not MCP. Found
via this module's own live test, not assumed: the MCP server's
`get_entities` tool exposes a `relatedDocuments` field that LOOKS like
institutionalMemory (same {start, count, total} shape) but is a different,
unrelated DataHub feature — it read back `total: 0` immediately after a
real institutionalMemory write a direct GraphQL query confirmed had
succeeded. `CLAUDE.md`'s actual rule is "MCP server OR SDK" for reads —
this is that "or," exercised for a checked reason, not a shortcut.

Scribe has NO LLM in it anywhere — it executes exactly what Investigator
already concluded, deterministically. Same reasoning as Sentinel's zero-LLM
detection math (lld-sprint2.md §1.4): there's nothing here that benefits
from judgment, only from correctly executing an already-made decision.
"""

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    AssertionInfoClass,
    AssertionResultClass,
    AssertionRunEventClass,
    AssertionSourceClass,
    AssertionStdParameterClass,
    AssertionStdParametersClass,
    AuditStampClass,
    DatasetAssertionInfoClass,
    GlobalTagsClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
    TagAssociationClass,
    TagPropertiesClass,
)

# Same load_dotenv() convention as every other module in this repo.
load_dotenv()

DATAHUB_SERVER = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
DATAHUB_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

# ---------------------------------------------------------------------------
# Constants — decision 0007.
# ---------------------------------------------------------------------------

GUARDIAN_TAG_URN = "urn:li:tag:guardian-incident"
GUARDIAN_TAG_NAME = "guardian-incident"
GUARDIAN_TAG_DESCRIPTION = (
    "Flagged by Claims Denial Guardian at least once. See the entity's "
    "documentation notes (institutionalMemory) for which specific "
    "incident(s) and why."
)

# This sprint's one concrete, scope-approved expectation (decision 0007) —
# NOT a generic parser over InvestigatorFinding.root_cause_summary's prose.
BILLING_AMOUNT_COLUMN = "billing_amount"
BILLING_AMOUNT_MIN = "0"


def _assertion_urn_for(table_name: str) -> str:
    """One assertion per (dataset, expectation) pair, reused across every
    incident that implicates that entity — not one per incident (decision
    0007). `table_name` is the bare table name (e.g. "claims"), matching
    what InvestigatorFinding.affected_branch actually contains.
    """
    return f"urn:li:assertion:guardian-billing-amount-non-negative-{table_name}"


# ---------------------------------------------------------------------------
# Result types.
# ---------------------------------------------------------------------------


@dataclass
class ScribeEntityResult:
    """What happened for one entity in affected_branch."""

    entity_name: str
    entity_urn: Optional[str]
    tag_applied: bool = False
    tag_already_present: bool = False
    doc_note_added: bool = False
    doc_note_already_present: bool = False
    assertion_defined: bool = False
    assertion_already_defined: bool = False
    assertion_run_event_emitted: bool = False
    skipped_reason: Optional[str] = None  # e.g. "entity not found", "no billing_amount column"


@dataclass
class ScribeResult:
    """run_scribe()'s return value — what Scribe actually did for one Incident."""

    incident_id: str
    entities: list[ScribeEntityResult] = field(default_factory=list)
    doc_url: Optional[str] = None  # the GitHub blob URL used, or None if it couldn't be constructed


# ---------------------------------------------------------------------------
# GitHub blob URL — read from the real git remote, never hardcoded
# (repo owner's explicit ask, Part B approval note 3).
# ---------------------------------------------------------------------------

_GITHUB_HTTPS_RE = re.compile(r"^https://github\.com/(?P<org>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")
_GITHUB_SSH_RE = re.compile(r"^git@github\.com:(?P<org>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")


def _github_blob_url(incident_id: str, repo_root: Path) -> Optional[str]:
    """Construct a GitHub blob URL to examples/<incident_id>/incident.json
    from the repo's ACTUAL `git remote get-url origin` output — not a
    hardcoded org/repo string. Returns None (not a guess) if the remote
    isn't a GitHub URL in a recognized shape, so a caller can degrade
    gracefully (still write the doc note, just without a clickable link)
    rather than write a broken URL.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None

    remote_url = result.stdout.strip()
    match = _GITHUB_HTTPS_RE.match(remote_url) or _GITHUB_SSH_RE.match(remote_url)
    if not match:
        return None

    return f"https://github.com/{match['org']}/{match['repo']}/blob/main/examples/{incident_id}/incident.json"


# ---------------------------------------------------------------------------
# MCP reads. Same session-spawning pattern as
# investigator.py's _investigate_design_a_async — duplicated deliberately,
# not extracted into a shared module this sprint (decision 0007's
# Consequences section states why).
# ---------------------------------------------------------------------------


async def _call_mcp_tool(session: ClientSession, tool_name: str, arguments: dict) -> dict:
    result = await session.call_tool(tool_name, arguments)
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    raw = "\n".join(parts)
    return json.loads(raw)


async def _resolve_entity_urn(session: ClientSession, table_name: str) -> Optional[str]:
    """Search-based discovery, same pattern add_lineage.py/add_metadata.py/
    register_ml_model.py already use (never construct the URN directly from
    the naming convention — verify it's real and live, per CLAUDE.md's
    "never hardcode schemas" rule extended to entity identity too)."""
    data = await _call_mcp_tool(session, "search", {"query": f"/q {table_name}", "filter": "entity_type = dataset", "num_results": 20})
    for result in data.get("searchResults", []):
        entity = result.get("entity", {})
        if entity.get("properties", {}).get("name") == table_name:
            return entity.get("urn")
    return None


async def _get_entity_details(session: ClientSession, urn: str) -> dict:
    """The ONE MCP read per entity everything else below derives from —
    tags, schema fields, and existing doc notes are all present in a single
    get_entities response (confirmed live during Part B's investigation),
    so this is called exactly once per entity in the main loop rather than
    once per fact needed.

    Returns the raw tool-call payload directly, NOT `payload["result"]` --
    a real bug found and fixed via Part C's live test, not assumed correct
    from Part B's exploration: querying this same MCP tool through Claude
    Code's own loaded tool interface (used during Part B's investigation)
    showed a `{"result": {...}}` wrapper, but calling the identical
    "get_entities" tool through the raw `mcp` Python SDK's
    `session.call_tool()` -- what this module and every real Scribe run
    actually uses -- returns the entity fields at the TOP level, no
    wrapper. Confirmed directly: a standalone script hitting this exact
    code path printed the raw JSON and showed `schemaMetadata` as a
    top-level key, not nested under `result`. The `{"result": ...}` shape
    was something Claude Code's own tool-calling layer added when
    presenting the response to me, not what the wire protocol/SDK actually
    returns to this module's code.
    """
    data = await _call_mcp_tool(session, "get_entities", {"urns": urn})
    return data


def _has_billing_amount_column(details: dict) -> bool:
    # `or {}` / `or []` throughout, not bare .get(key, default): a GraphQL
    # nullable field returns an explicit `null` (Python None) when an entity
    # genuinely has no schema/tags registered, not just an absent key --
    # .get(key, default) only substitutes the default for a MISSING key, not
    # a present key whose value is None.
    fields = (details.get("schemaMetadata") or {}).get("fields") or []
    return any(f.get("fieldPath") == BILLING_AMOUNT_COLUMN for f in fields)


def _current_tag_urns(details: dict) -> set[str]:
    tags = (details.get("tags") or {}).get("tags") or []
    return {t["tag"]["urn"] for t in tags if "tag" in t and "urn" in t["tag"]}


def _read_institutional_memory(graph, urn: str) -> dict:
    """SDK/GraphQL read, NOT MCP — a real, verified exception to decision
    0003's usual read-via-MCP pattern for agents, found (not assumed) via
    Part C's live test: the MCP server's `get_entities` tool exposes a
    `relatedDocuments` field that LOOKS like it could be institutionalMemory
    (same {start, count, total} shape) but is actually a different,
    unrelated DataHub feature — it read back `total: 0` immediately after a
    real institutionalMemory write that a direct GraphQL query confirmed
    HAD succeeded. The MCP tool schema has no parameter to request
    institutionalMemory specifically; it simply isn't in this server's
    fixed per-entity-type field set for datasets. `CLAUDE.md`'s actual rule
    is "MCP server OR SDK" for reads — this is that "or" being exercised
    for a real, checked reason, matching the precedent
    register_ml_model.py's docstring already sets for documented,
    server-verified limitations (there: MLModel can't be a real lineage
    upstream; here: this MCP server can't read back institutionalMemory).
    """
    result = graph.execute_graphql(
        "query($urn: String!) { dataset(urn: $urn) { institutionalMemory { elements { url description created { time } } } } }",
        variables={"urn": urn},
    )
    return (result.get("dataset") or {}).get("institutionalMemory") or {}


def _parse_doc_entries(institutional_memory: dict) -> tuple[set[str], list[InstitutionalMemoryMetadataClass]]:
    """Which incident_ids already have a doc note on this entity, and the
    full existing element list (needed to write back a superset, not a
    replacement — institutionalMemory is a whole-list aspect, decision
    0007). Takes the real institutionalMemory GraphQL shape (see
    _read_institutional_memory), not the MCP get_entities response.
    Descriptions are expected to start with "[<incident_id>] " (see
    _build_doc_description) — parsed back out here for the dedup check.
    """
    ids: set[str] = set()
    elements: list[InstitutionalMemoryMetadataClass] = []
    for entry in institutional_memory.get("elements", []) or []:
        desc = entry.get("description", "") or ""
        m = re.match(r"^\[(INC-[^\]]+)\]", desc)
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


async def _assertion_already_exists(session: ClientSession, assertion_urn: str) -> bool:
    try:
        data = await _call_mcp_tool(session, "get_entities", {"urns": assertion_urn})
    except Exception:
        return False
    # No "result" wrapper -- see _get_entity_details' docstring for why.
    # A nonexistent assertion still round-trips as {"urn": ..., "type": "ASSERTION"}
    # with no "info" key from this server — a real, registered assertion has "info".
    return "info" in data


# ---------------------------------------------------------------------------
# SDK writes (decision 0003: writes use the REST emitter + DATAHUB_GMS_TOKEN,
# not MCP — mutation tools are disabled by default on the MCP server anyway).
# ---------------------------------------------------------------------------


def _apply_incident_tag(emitter: DatahubRestEmitter, entity_urn: str, current_tags: set[str]) -> bool:
    """Set-union idempotency (decision 0007): only write if not already
    present. Returns True if a write happened."""
    if GUARDIAN_TAG_URN in current_tags:
        return False
    new_tags = current_tags | {GUARDIAN_TAG_URN}
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=entity_urn,
            aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=t) for t in sorted(new_tags)]),
        )
    )
    return True


def _ensure_guardian_tag_exists(emitter: DatahubRestEmitter) -> None:
    """Idempotent by nature (identical content re-emitted is harmless) —
    called once per run, same "batch pattern" convention add_metadata.py's
    create_tags() already uses."""
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=GUARDIAN_TAG_URN,
            aspect=TagPropertiesClass(name=GUARDIAN_TAG_NAME, description=GUARDIAN_TAG_DESCRIPTION),
        )
    )


def _build_doc_description(incident_id: str, entity_name: str, investigator_finding) -> str:
    """"[<incident_id>] " prefix is the dedup key _parse_doc_entries()
    parses back out — not just cosmetic."""
    breakdown_for_entity = [
        e for e in investigator_finding.root_cause_breakdown if entity_name in e.classification
    ]
    if breakdown_for_entity:
        detail = "; ".join(f"{e.classification}: {e.claim_count} claims ({e.pct}%)" for e in breakdown_for_entity)
    else:
        detail = investigator_finding.primary_root_cause
    return (
        f"[{incident_id}] Guardian incident — {detail}. "
        f"Confidence: {investigator_finding.confidence}. Full finding: see linked incident.json."
    )


def _append_incident_doc_note(
    emitter: DatahubRestEmitter,
    entity_urn: str,
    entity_name: str,
    existing_elements: list[InstitutionalMemoryMetadataClass],
    incident_id: str,
    investigator_finding,
    doc_url: Optional[str],
) -> bool:
    """No internal already-present check: the caller (_run_scribe_async)
    already gates on incident_id in existing_doc_ids before ever calling
    this — unlike _apply_incident_tag, which is cheap to make doubly-safe
    since it takes the same current_tags value the caller already checked.
    Here, checking again would mean re-deriving existing_doc_ids from
    existing_elements a second time for no real safety benefit, since
    existing_elements is only ever passed by the one call site that already
    did the check. Always writes when called.
    """
    now_ms = int(time.time() * 1000)
    new_entry = InstitutionalMemoryMetadataClass(
        url=doc_url or f"urn:li:corpuser:guardian#{incident_id}",  # see module docstring: doc_url may be None
        description=_build_doc_description(incident_id, entity_name, investigator_finding),
        createStamp=AuditStampClass(time=now_ms, actor="urn:li:corpuser:datahub"),
    )
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=entity_urn,
            aspect=InstitutionalMemoryClass(elements=existing_elements + [new_entry]),
        )
    )
    return True


def _ensure_assertion_defined(emitter: DatahubRestEmitter, assertion_urn: str, dataset_urn: str, entity_name: str) -> None:
    """`entity_name` (the bare table name, e.g. "claims") is included in the
    description text itself, not just encoded in the URN — found necessary
    during Part D's hands-on UAT: two assertions on different datasets with
    word-for-word identical descriptions ("...on this dataset.") read as
    "the same assertion" at a glance in the DataHub UI, even though they're
    genuinely distinct entities with their own URNs and run-event histories.
    The URN alone was correct; the human-facing text needed the same
    specificity.
    """
    field_urn = f"urn:li:schemaField:({dataset_urn},{BILLING_AMOUNT_COLUMN})"
    info = AssertionInfoClass(
        type="DATASET",
        datasetAssertion=DatasetAssertionInfoClass(
            dataset=dataset_urn,
            scope="DATASET_COLUMN",
            operator="GREATER_THAN_OR_EQUAL_TO",
            fields=[field_urn],
            parameters=AssertionStdParametersClass(
                value=AssertionStdParameterClass(value=BILLING_AMOUNT_MIN, type="NUMBER")
            ),
        ),
        source=AssertionSourceClass(type="INFERRED"),
        description=f"Guardian: {BILLING_AMOUNT_COLUMN} must never be negative on {entity_name}.",
    )
    emitter.emit(MetadataChangeProposalWrapper(entityUrn=assertion_urn, aspect=info))


def _emit_assertion_run_event(
    emitter: DatahubRestEmitter,
    assertion_urn: str,
    dataset_urn: str,
    incident_id: str,
    incident_created_at: str,
    unexpected_count: int,
) -> None:
    """runId=incident_id, timestampMillis derived from the incident's own
    created_at — both deterministic, per decision 0007's EMPIRICALLY
    MEASURED finding (not assumed): re-emitting the identical
    (assertionUrn, timestampMillis, runId) tuple was confirmed live to not
    create a duplicate in DataHub's timeseries store."""
    dt = datetime.fromisoformat(incident_created_at)
    timestamp_millis = int(dt.timestamp() * 1000)
    run_event = AssertionRunEventClass(
        timestampMillis=timestamp_millis,
        runId=incident_id,
        asserteeUrn=dataset_urn,
        status="COMPLETE",
        assertionUrn=assertion_urn,
        result=AssertionResultClass(type="FAILURE", unexpectedCount=unexpected_count),
    )
    emitter.emit(MetadataChangeProposalWrapper(entityUrn=assertion_urn, aspect=run_event))


def _unexpected_count_for(entity_name: str, investigator_finding) -> int:
    """Pull the claim_count for whichever root_cause_breakdown entry
    implicates this specific entity — e.g. for
    "introduced_at:claims (sign-flip bug...)" against entity_name="claims".
    Falls back to 0 (still emits the run event — a FAILURE with an unknown
    count is still real signal) if no breakdown entry names this entity,
    which shouldn't happen for an entity Investigator itself put in
    affected_branch, but isn't assumed to be impossible.
    """
    total = 0
    matched = False
    for entry in investigator_finding.root_cause_breakdown:
        if entity_name in entry.classification:
            total += entry.claim_count
            matched = True
    return total if matched else 0


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


async def _run_scribe_async(incident, repo_root: Path) -> ScribeResult:
    finding = incident.investigator
    result = ScribeResult(incident_id=incident.incident_id)

    if finding is None or not finding.affected_branch:
        # Nothing to write back -- a clean run (no_anomaly) or an
        # inconclusive one with no implicated entities. Handled by simply
        # having nothing to iterate, not a special case.
        return result

    doc_url = _github_blob_url(incident.incident_id, repo_root)
    result.doc_url = doc_url

    server_params = StdioServerParameters(
        command="uvx",
        args=["mcp-server-datahub@latest"],
        env={**os.environ, "DATAHUB_GMS_URL": DATAHUB_SERVER, "DATAHUB_GMS_TOKEN": DATAHUB_TOKEN or ""},
    )

    emitter = DatahubRestEmitter(DATAHUB_SERVER, token=DATAHUB_TOKEN)
    # SDK read client, used ONLY for _read_institutional_memory (see that
    # function's docstring for why this one read doesn't go through MCP).
    graph = DataHubGraph(DatahubClientConfig(server=DATAHUB_SERVER, token=DATAHUB_TOKEN))
    _ensure_guardian_tag_exists(emitter)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            for entity_name in finding.affected_branch:
                entity_urn = await _resolve_entity_urn(session, entity_name)
                if entity_urn is None:
                    result.entities.append(
                        ScribeEntityResult(entity_name=entity_name, entity_urn=None, skipped_reason="entity not found in DataHub")
                    )
                    continue

                entity_result = ScribeEntityResult(entity_name=entity_name, entity_urn=entity_urn)
                details = await _get_entity_details(session, entity_urn)

                # --- 1. Tag ---
                current_tags = _current_tag_urns(details)
                if GUARDIAN_TAG_URN in current_tags:
                    entity_result.tag_already_present = True
                else:
                    entity_result.tag_applied = _apply_incident_tag(emitter, entity_urn, current_tags)

                # --- 2. Doc note (SDK read, not MCP -- see
                # _read_institutional_memory's docstring) ---
                institutional_memory = _read_institutional_memory(graph, entity_urn)
                existing_doc_ids, existing_elements = _parse_doc_entries(institutional_memory)
                if incident.incident_id in existing_doc_ids:
                    entity_result.doc_note_already_present = True
                else:
                    entity_result.doc_note_added = _append_incident_doc_note(
                        emitter=emitter,
                        entity_urn=entity_urn,
                        entity_name=entity_name,
                        existing_elements=existing_elements,
                        incident_id=incident.incident_id,
                        investigator_finding=finding,
                        doc_url=doc_url,
                    )

                # --- 3. Assertion (only if this entity actually has billing_amount) ---
                if _has_billing_amount_column(details):
                    assertion_urn = _assertion_urn_for(entity_name)
                    if await _assertion_already_exists(session, assertion_urn):
                        entity_result.assertion_already_defined = True
                    else:
                        _ensure_assertion_defined(emitter, assertion_urn, entity_urn, entity_name)
                        entity_result.assertion_defined = True

                    unexpected_count = _unexpected_count_for(entity_name, finding)
                    _emit_assertion_run_event(
                        emitter, assertion_urn, entity_urn, incident.incident_id, incident.created_at, unexpected_count,
                    )
                    entity_result.assertion_run_event_emitted = True
                else:
                    entity_result.skipped_reason = f"no {BILLING_AMOUNT_COLUMN} column — assertion not applicable"

                result.entities.append(entity_result)

    return result


def run_scribe(incident, repo_root: Optional[Path] = None) -> ScribeResult:
    """Top-level entry point, matching sentinel.py's run_sentinel() /
    investigator.py's run_investigator() naming convention. `repo_root`
    defaults to this file's own repo (three levels up from src/agents/)."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
    return asyncio.run(_run_scribe_async(incident, repo_root))
