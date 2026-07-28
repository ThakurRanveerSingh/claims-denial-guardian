#!/usr/bin/env python3
"""
FHIR Compliance Bridge — Sprint 3 stretch, WP5. Design: docs/decisions/
0012-fhir-compliance-bridge.md. Read that file for the full reasoning; this
module implements it, not re-derives it.

CMS-0057-F requires FHIR-based prior-auth/claims data exchange (denial-metrics
reporting mandatory since Jan 2026, full API compliance due Jan 2027). This
is a THIN compliance-LINKAGE demo, not a production FHIR server: it generates
a small, deterministic sample of FHIR R4 ExplanationOfBenefit resources for
an already-investigated incident's segment, stamps each one with a Guardian
data-quality extension linking it back to the incident, and registers the
export as a DataHub dataset with lineage to raw_patients — so a CMS-facing
resource can be traced directly to the data Guardian has already flagged as
under active quality investigation.

Two entry points, split by concern the same way Scribe/Drift already are in
this codebase:
  - `run_fhir_export()` — pure, zero-LLM, zero-DataHub: reads a sample of the
    incident's segment claims from the local healthcare.db, deterministically
    templates each into an EOB resource, writes them to
    examples/<incident_id>/fhir/. Translation, not judgment — same law as
    Sentinel/Scribe's zero-LLM discipline.
  - `run_fhir_writeback()` — extends Scribe's exact tag/doc-note pattern
    (decision 0007) onto ONE persistent DataHub dataset entity representing
    the export artifact (not one entity per incident/claim), plus a one-time
    lineage edge back to raw_patients. Same "SDK writes, MCP reads" split
    (decision 0003).

Honesty discipline (decision 0012 has the full list): no ICD-10/CARC codes
are fabricated anywhere — `medical_condition`/`denial_reason_code` are
carried as `.text` only. `type` (a required CodeableConcept this project has
no real data for) is represented via FHIR's own `data-absent-reason`
extension mechanism, not a plausible-looking guess. `patient`/`insurer`/
`provider` are display-only References — no Patient/Organization resources
exist (explicitly out of scope), so no fake resolvable reference is emitted
either.
"""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    GlobalTagsClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
    TagAssociationClass,
    TagPropertiesClass,
    UpstreamClass,
    UpstreamLineageClass,
)

load_dotenv()

DATAHUB_SERVER = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
DATAHUB_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")

# ---------------------------------------------------------------------------
# Constants — decision 0012.
# ---------------------------------------------------------------------------

FHIR_VERSION = "4.0.1"  # R4
DATA_ABSENT_REASON_EXT_URL = "http://hl7.org/fhir/StructureDefinition/data-absent-reason"

# Sprint 3 WP4/decision 0007's precedent: one concrete, scope-approved
# expectation, not a generic reason-code mapper. Both canonical incidents'
# flagged root cause is exactly the INVALID_BILLING_AMOUNT-denied claims in
# their segment (confirmed against incident.json's own root_cause_breakdown
# before hardcoding this) — sampling THOSE specific claims is what makes the
# exported EOB visibly carry the defect Guardian is investigating, not an
# arbitrary claim from the segment.
DENIAL_REASON_SAMPLED = "INVALID_BILLING_AMOUNT"
DEFAULT_SAMPLE_LIMIT = 3

# One persistent dataset entity for the export ARTIFACT TYPE, not one per
# incident/claim — same "reuse, don't multiply" discipline as Scribe's single
# guardian-incident tag (decision 0007). Platform "file" is the honest label:
# this is JSON written to disk, not a queryable table (mirrors
# register_ml_model.py's "python" platform for a model that isn't tied to
# any ML framework — the label says what it actually is).
FHIR_EXPORT_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:file,healthcare.guardian_exports.fhir_explanation_of_benefit,PROD)"
)
FHIR_EXPORT_DATASET_NAME = "healthcare.guardian_exports.fhir_explanation_of_benefit"

GUARDIAN_FHIR_TAG_URN = "urn:li:tag:guardian-fhir-export"
GUARDIAN_FHIR_TAG_NAME = "guardian-fhir-export"
GUARDIAN_FHIR_TAG_DESCRIPTION = (
    "Guardian has exported at least one sample FHIR R4 ExplanationOfBenefit resource built on data "
    "linked to this incident. See the entity's documentation notes (institutionalMemory) for which "
    "incident(s). Structural demo only — not validated against Da Vinci PAS/PDex profiles "
    "(docs/decisions/0012)."
)

RAW_PATIENTS_TABLE_NAME = "raw_patients"


# ---------------------------------------------------------------------------
# Result types.
# ---------------------------------------------------------------------------


@dataclass
class FhirClaimSample:
    claim_id: str
    resource_path: str
    resource: dict = field(default_factory=dict)


@dataclass
class FhirExportResult:
    incident_id: str
    samples: list = field(default_factory=list)  # list[FhirClaimSample]
    output_dir: Optional[str] = None
    denial_reason_code_sampled: str = DENIAL_REASON_SAMPLED
    note: Optional[str] = None  # e.g. "no matching claims found" — quarantine-not-hide, decision 0008's discipline


@dataclass
class FhirWritebackResult:
    incident_id: str
    entity_urn: Optional[str] = None
    tag_applied: bool = False
    tag_already_present: bool = False
    doc_note_added: bool = False
    doc_note_already_present: bool = False
    upstream_resolved: Optional[str] = None  # raw_patients URN actually resolved live, or None
    skipped_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# GitHub base URL — same live-git-remote pattern scribe.py's
# `_github_blob_url` already established, duplicated per this codebase's
# "small per-module boilerplate, copied not shared" convention (scribe.py/
# drift.py's own docstrings already state why). Used both for the incident
# evidence link AND as the namespace for this module's own custom FHIR
# extension — never a hardcoded org/repo string.
# ---------------------------------------------------------------------------

_GITHUB_HTTPS_RE = re.compile(r"^https://github\.com/(?P<org>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")
_GITHUB_SSH_RE = re.compile(r"^git@github\.com:(?P<org>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")


def _github_repo_base_url(repo_root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=str(repo_root), capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    remote_url = result.stdout.strip()
    match = _GITHUB_HTTPS_RE.match(remote_url) or _GITHUB_SSH_RE.match(remote_url)
    if not match:
        return None
    return f"https://github.com/{match['org']}/{match['repo']}"


def _github_blob_url(incident_id: str, repo_root: Path) -> Optional[str]:
    base = _github_repo_base_url(repo_root)
    if base is None:
        return None
    return f"{base}/blob/main/examples/{incident_id}/incident.json"


def _extension_base_url(repo_root: Path) -> str:
    """Canonical namespace for this module's custom extension/tag/identifier
    system URIs. Points at the decision doc that documents what the
    extension means — a real, resolvable page, not a fake `/fhir/` path that
    would imply a hosted, registered profile that doesn't exist. Degrades to
    a non-resolving `urn:` (still a valid, if not dereferenceable, URI —
    honest about not resolving rather than fabricating a GitHub org) if no
    GitHub remote is configured, same graceful-degradation shape as
    scribe.py's own doc_url handling."""
    base = _github_repo_base_url(repo_root)
    if base is None:
        return "urn:guardian:fhir"
    return f"{base}/blob/main/docs/decisions/0012-fhir-compliance-bridge.md"


# ---------------------------------------------------------------------------
# Part 1: EOB generation — pure, deterministic, zero LLM.
# ---------------------------------------------------------------------------


def _fetch_sample_claims(db_path: Path, provider: str, condition: str, limit: int) -> list:
    """Real denied claims for this incident's segment, filtered to the ONE
    denial reason DENIAL_REASON_SAMPLED implicates (see that constant's
    comment) — not an arbitrary sample across all reason codes, so every
    exported EOB visibly carries the defect under investigation.
    `ORDER BY c.claim_id` makes the sample deterministic run-to-run (same
    limit -> same claims -> byte-identical resources), which is what makes
    idempotency actually provable, not just claimed."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT c.claim_id, c.patient_name, c.hospital, c.insurance_provider, c.medical_condition,
                   c.billing_amount, c.date_of_admission, c.discharge_date, c.admission_type, c.medication,
                   d.denial_date, d.denial_reason_code, d.denial_amount
            FROM claims c JOIN denials d ON d.claim_id = c.claim_id
            WHERE c.insurance_provider = ? AND c.medical_condition = ? AND d.denial_reason_code = ?
            ORDER BY c.claim_id
            LIMIT ?
            """,
            (provider, condition, DENIAL_REASON_SAMPLED, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _build_eob_resource(claim: dict, incident, repo_root: Path) -> dict:
    """One FHIR R4 ExplanationOfBenefit per denied claim. Every element
    below is either (a) real data carried through as-is/`.text`-only, (b)
    a fixed, honestly-true structural value (`status`, `use`, `outcome`),
    or (c) `type`'s explicit data-absent-reason extension — see module
    docstring and decision 0012 for the full real-vs-placeholder accounting.
    No ICD-10/CARC/RxNorm code is fabricated anywhere in this function.
    """
    finding = incident.investigator
    ext_base = _extension_base_url(repo_root)
    evidence_url = _github_blob_url(incident.incident_id, repo_root)
    resource_id = claim["claim_id"].lower()  # "CLM-000183" -> "clm-000183", already valid FHIR id chars

    quality_flag_extension = {
        "url": f"{ext_base}#guardian-data-quality-flag",
        "extension": [
            {"url": "incidentId", "valueString": incident.incident_id},
            # Verbatim from InvestigatorFinding — this IS the
            # introduced_at:claims vs inherited_from:raw_patients
            # distinction the design asked for, not a re-derived summary.
            {"url": "classification", "valueString": finding.primary_root_cause},
            {"url": "confidence", "valueString": finding.confidence},
        ],
    }
    if evidence_url:
        quality_flag_extension["extension"].append({"url": "evidence", "valueUrl": evidence_url})

    return {
        "resourceType": "ExplanationOfBenefit",
        "id": resource_id,
        "meta": {
            "tag": [
                {
                    "system": f"{ext_base}#tags",
                    "code": "guardian-flagged",
                    "display": "Built on data currently under active Guardian quality investigation",
                }
            ]
        },
        "extension": [quality_flag_extension],
        "identifier": [{"system": f"{ext_base}#claim-id", "value": claim["claim_id"]}],
        "status": "active",
        # Required (1..1) CodeableConcept — no real claim-type classification
        # exists in this project's source data (no institutional/professional
        # distinction anywhere in claims). Rather than guess a plausible-
        # looking code, this uses FHIR's own data-absent-reason mechanism:
        # the element is present (satisfies cardinality) but carries only the
        # absent-reason extension, no coding/text. "unsupported" is the
        # correct code here (per the HL7 valueset) — the SOURCE SYSTEM
        # doesn't capture this, not merely "unknown for this one record."
        "type": {"extension": [{"url": DATA_ABSENT_REASON_EXT_URL, "valueCode": "unsupported"}]},
        "use": "claim",  # real: these are retrospective denied claims, not prior-auth requests
        "patient": {"display": claim["patient_name"]},  # display-only -- no Patient resource exists (out of scope)
        "created": claim["denial_date"],  # already ISO 8601 (YYYY-MM-DD) in the source data, no reformatting needed
        "insurer": {"display": claim["insurance_provider"]},
        "provider": {"display": claim["hospital"]},
        "outcome": "complete",  # real FHIR semantics: adjudication finished (denial itself is in `disposition`/adjudication, not `outcome`)
        "disposition": f"Denied — {claim['denial_reason_code']} (see extension for Guardian root-cause linkage)",
        "billablePeriod": {"start": claim["date_of_admission"], "end": claim["discharge_date"]},
        "diagnosis": [
            {
                "sequence": 1,
                # `.text` ONLY -- no `.coding`. No real ICD-10 mapping exists
                # for this project's `medical_condition` free-text field; a
                # fabricated code would look authoritative and wouldn't be.
                "diagnosisCodeableConcept": {"text": claim["medical_condition"]},
            }
        ],
        "insurance": [{"focal": True, "coverage": {"display": claim["insurance_provider"]}}],
        "item": [
            {
                "sequence": 1,
                "productOrService": {"text": claim["medication"] or "(not recorded)"},
                "net": {"value": claim["billing_amount"], "currency": "USD"},
                "adjudication": [
                    {
                        # `.text` only -- denial_reason_code is this project's
                        # own internal enum, not a real CARC/RARC code.
                        "category": {"text": "denial-reason"},
                        "reason": {"text": claim["denial_reason_code"]},
                        "amount": {"value": claim["denial_amount"], "currency": "USD"},
                    }
                ],
            }
        ],
        "total": [{"category": {"text": "submitted"}, "amount": {"value": claim["billing_amount"], "currency": "USD"}}],
    }


def run_fhir_export(
    incident,
    healthcare_db_path: Optional[Path] = None,
    limit: int = DEFAULT_SAMPLE_LIMIT,
    repo_root: Optional[Path] = None,
    examples_dir: Optional[Path] = None,
) -> FhirExportResult:
    """Top-level entry point, matching scribe.py/drift.py's run_*() naming
    convention. Pure — no DataHub write; see run_fhir_writeback() for that."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
    if healthcare_db_path is None:
        healthcare_db_path = (repo_root / "src" / "datahub" / "healthcare.db").resolve()
    if examples_dir is None:
        examples_dir = repo_root / "examples"

    if incident.investigator is None:
        raise ValueError(f"{incident.incident_id} has no InvestigatorFinding to export FHIR resources from")

    segment = incident.sentinel.segment
    claims = _fetch_sample_claims(healthcare_db_path, segment.insurance_provider, segment.medical_condition, limit)

    result = FhirExportResult(incident_id=incident.incident_id, denial_reason_code_sampled=DENIAL_REASON_SAMPLED)
    if not claims:
        result.note = (
            f"No {DENIAL_REASON_SAMPLED}-denied claims found for "
            f"{segment.insurance_provider}/{segment.medical_condition} — nothing exported."
        )
        return result

    output_dir = examples_dir / incident.incident_id / "fhir"
    output_dir.mkdir(parents=True, exist_ok=True)
    result.output_dir = str(output_dir)

    for claim in claims:
        resource = _build_eob_resource(claim, incident, repo_root)
        path = output_dir / f"eob-{claim['claim_id'].lower()}.json"
        path.write_text(json.dumps(resource, indent=2))
        result.samples.append(FhirClaimSample(claim_id=claim["claim_id"], resource_path=str(path), resource=resource))

    return result


# ---------------------------------------------------------------------------
# Part 2: DataHub writeback — extends Scribe's exact tag/doc-note pattern
# (decision 0007) onto FHIR_EXPORT_DATASET_URN, plus a one-time lineage edge.
# Same "MCP reads, SDK writes" split (decision 0003), same session-spawning
# pattern scribe.py/drift.py already use (duplicated deliberately).
# ---------------------------------------------------------------------------


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command="uvx",
        args=["mcp-server-datahub@latest"],
        env={**os.environ, "DATAHUB_GMS_URL": DATAHUB_SERVER, "DATAHUB_GMS_TOKEN": DATAHUB_TOKEN or ""},
    )


async def _call_mcp_tool(session: ClientSession, tool_name: str, arguments: dict) -> dict:
    result = await session.call_tool(tool_name, arguments)
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    raw = "\n".join(parts)
    return json.loads(raw)


async def _resolve_entity_urn(session: ClientSession, table_name: str) -> Optional[str]:
    """Search-based discovery, same pattern scribe.py's own
    `_resolve_entity_urn` already uses — never construct raw_patients' URN
    directly from the naming convention."""
    data = await _call_mcp_tool(session, "search", {"query": f"/q {table_name}", "filter": "entity_type = dataset", "num_results": 20})
    for result in data.get("searchResults", []):
        entity = result.get("entity", {})
        if entity.get("properties", {}).get("name") == table_name:
            return entity.get("urn")
    return None


def _current_tag_urns(details: dict) -> set:
    tags = (details.get("tags") or {}).get("tags") or []
    return {t["tag"]["urn"] for t in tags if "tag" in t and "urn" in t["tag"]}


def _read_institutional_memory(graph, urn: str) -> dict:
    """SDK/GraphQL read, not MCP — same verified exception scribe.py's own
    `_read_institutional_memory` documents (the MCP server's
    `relatedDocuments` field is a different, unrelated feature)."""
    result = graph.execute_graphql(
        "query($urn: String!) { dataset(urn: $urn) { institutionalMemory { elements { url description created { time } } } } }",
        variables={"urn": urn},
    )
    return (result.get("dataset") or {}).get("institutionalMemory") or {}


def _parse_doc_entries(institutional_memory: dict):
    """Which incident_ids already have a doc note on this entity, and the
    full existing element list (institutionalMemory is a whole-list aspect
    — decision 0007). Same shape as scribe.py's `_parse_doc_entries`."""
    ids: set = set()
    elements: list = []
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


def _ensure_fhir_dataset_registered(emitter: DatahubRestEmitter) -> None:
    """DatasetPropertiesClass emit is a full-aspect overwrite of identical
    content on every call — idempotent by construction, same convention
    add_lineage.py/register_ml_model.py's own scripts already rely on (no
    "already registered" check needed, unlike the tag/doc-note writes below
    which have real per-incident state to dedup)."""
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=FHIR_EXPORT_DATASET_URN,
            aspect=DatasetPropertiesClass(
                name=FHIR_EXPORT_DATASET_NAME,
                description=(
                    "Sample FHIR R4 ExplanationOfBenefit resources generated by `guardian export-fhir` "
                    "(CMS-0057-F compliance-linkage demo, Sprint 3 WP5). Each resource carries a "
                    "guardian-data-quality-flag extension linking it to the Guardian incident whose "
                    "investigated claims it was built from. Structural demo only — not a FHIR server, "
                    "not validated against Da Vinci PAS/PDex profiles. See docs/decisions/0012."
                ),
                customProperties={"fhir_version": FHIR_VERSION, "resource_type": "ExplanationOfBenefit"},
            ),
        )
    )


def _ensure_fhir_tag_exists(emitter: DatahubRestEmitter) -> None:
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=GUARDIAN_FHIR_TAG_URN,
            aspect=TagPropertiesClass(name=GUARDIAN_FHIR_TAG_NAME, description=GUARDIAN_FHIR_TAG_DESCRIPTION),
        )
    )


def _apply_fhir_tag(emitter: DatahubRestEmitter, current_tags: set) -> bool:
    if GUARDIAN_FHIR_TAG_URN in current_tags:
        return False
    new_tags = current_tags | {GUARDIAN_FHIR_TAG_URN}
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=FHIR_EXPORT_DATASET_URN,
            aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=t) for t in sorted(new_tags)]),
        )
    )
    return True


def _build_fhir_doc_description(incident, sample_count: int) -> str:
    """"[<incident_id>] " prefix is the dedup key `_parse_doc_entries()`
    parses back out — same convention as scribe.py's `_build_doc_description`."""
    finding = incident.investigator
    segment = incident.sentinel.segment
    return (
        f"[{incident.incident_id}] {sample_count} sample ExplanationOfBenefit resource(s) exported for "
        f"{segment.insurance_provider}/{segment.medical_condition}, sourced from claims/denials records "
        f"with denial_reason_code={DENIAL_REASON_SAMPLED} matching Investigator's root-cause finding "
        f"({finding.primary_root_cause}, confidence: {finding.confidence}). See linked incident.json and "
        f"examples/{incident.incident_id}/fhir/ for the generated resources. Structural demo only — not "
        f"validated against Da Vinci PAS/PDex profiles (docs/decisions/0012)."
    )


def _append_fhir_doc_note(emitter: DatahubRestEmitter, existing_elements: list, incident, doc_url: Optional[str], sample_count: int) -> bool:
    import time

    now_ms = int(time.time() * 1000)
    new_entry = InstitutionalMemoryMetadataClass(
        url=doc_url or f"urn:li:corpuser:guardian#{incident.incident_id}",
        description=_build_fhir_doc_description(incident, sample_count),
        createStamp=AuditStampClass(time=now_ms, actor="urn:li:corpuser:datahub"),
    )
    emitter.emit(
        MetadataChangeProposalWrapper(
            entityUrn=FHIR_EXPORT_DATASET_URN,
            aspect=InstitutionalMemoryClass(elements=existing_elements + [new_entry]),
        )
    )
    return True


async def _run_fhir_writeback_async(incident, export_result: FhirExportResult, repo_root: Path) -> FhirWritebackResult:
    result = FhirWritebackResult(incident_id=incident.incident_id, entity_urn=FHIR_EXPORT_DATASET_URN)
    doc_url = _github_blob_url(incident.incident_id, repo_root)

    emitter = DatahubRestEmitter(DATAHUB_SERVER, token=DATAHUB_TOKEN)
    graph = DataHubGraph(DatahubClientConfig(server=DATAHUB_SERVER, token=DATAHUB_TOKEN))

    _ensure_fhir_dataset_registered(emitter)
    _ensure_fhir_tag_exists(emitter)

    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # --- Lineage: FHIR_EXPORT_DATASET_URN <- raw_patients, resolved
            # live (never hardcoded — CLAUDE.md's "never hardcode schemas"
            # rule extended to entity identity, same as scribe.py). ---
            raw_patients_urn = await _resolve_entity_urn(session, RAW_PATIENTS_TABLE_NAME)
            if raw_patients_urn is not None:
                emitter.emit(
                    MetadataChangeProposalWrapper(
                        entityUrn=FHIR_EXPORT_DATASET_URN,
                        aspect=UpstreamLineageClass(
                            upstreams=[UpstreamClass(dataset=raw_patients_urn, type=DatasetLineageTypeClass.TRANSFORMED)]
                        ),
                    )
                )
                result.upstream_resolved = raw_patients_urn
            else:
                result.skipped_reason = f"{RAW_PATIENTS_TABLE_NAME} not found in DataHub — lineage skipped (dataset/tag/doc note still applied)"

            # --- Tag ---
            details = await _call_mcp_tool(session, "get_entities", {"urns": FHIR_EXPORT_DATASET_URN})
            current_tags = _current_tag_urns(details)
            if GUARDIAN_FHIR_TAG_URN in current_tags:
                result.tag_already_present = True
            else:
                result.tag_applied = _apply_fhir_tag(emitter, current_tags)

            # --- Doc note (per incident, same dedup convention as Scribe) ---
            institutional_memory = _read_institutional_memory(graph, FHIR_EXPORT_DATASET_URN)
            existing_ids, existing_elements = _parse_doc_entries(institutional_memory)
            if incident.incident_id in existing_ids:
                result.doc_note_already_present = True
            else:
                result.doc_note_added = _append_fhir_doc_note(emitter, existing_elements, incident, doc_url, len(export_result.samples))

    return result


def run_fhir_writeback(incident, export_result: FhirExportResult, repo_root: Optional[Path] = None) -> FhirWritebackResult:
    """Top-level entry point for the write side — matching scribe.py/
    drift.py's run_*_writeback() naming convention."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
    return asyncio.run(_run_fhir_writeback_async(incident, export_result, repo_root))
