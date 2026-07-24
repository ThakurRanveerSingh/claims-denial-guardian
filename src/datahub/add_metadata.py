#!/usr/bin/env python3
"""
Add metadata (tags, glossary terms, ownership) to Healthcare dataset in DataHub.

Run AFTER ingestion + lineage:
    datahub ingest -c ingest.yaml
    python add_lineage.py
    python add_metadata.py

Supports: --dry-run
"""

import os
import sys
import time

from dotenv import load_dotenv

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    GlobalTagsClass,
    GlossaryTermAssociationClass,
    GlossaryTermInfoClass,
    GlossaryTermsClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    TagAssociationClass,
    TagPropertiesClass,
)

# See add_lineage.py for why this is required now (docs/decisions/0003).
load_dotenv()

DATAHUB_SERVER = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
DATAHUB_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")
PLATFORM = "sqlite"
DEFAULT_INSTANCE = "healthcare"
VALID_INSTANCES = ["healthcare"]

# ─── Tags ───
TAG_DEFINITIONS = {
    "pii": {
        "description": "Contains personally identifiable information (patient names, ages, medical conditions).",
    },
    "critical": {
        "description": "Business-critical table. Data quality issues here have direct financial or operational impact.",
    },
    "internal": {
        "description": "Internal-use table for research and analytics. Lower severity if quality degrades.",
    },
    "quality_monitored": {
        "description": "Table with active quality monitoring. Quality assertions are expected to run against this data.",
    },
    "pipeline_stage": {
        "description": "Part of a multi-stage data pipeline (raw → staging → mart).",
    },
}

TAG_ASSIGNMENTS = {
    "pii": ["raw_patients", "staging_patients", "mart_demographics"],
    # denials added: same direct-financial-impact reasoning as mart_billing.
    "critical": ["mart_billing", "denials"],
    "internal": ["mart_demographics"],
    # claims/denials added: Sentinel actively checks these (LLD §4 step 3).
    "quality_monitored": ["raw_patients", "claims", "denials"],
    "pipeline_stage": [
        "raw_patients", "staging_patients", "mart_billing", "mart_demographics",
        "claims", "denials", "denial_model_scores",
    ],
}

# ─── Glossary ───
GLOSSARY_DEFINITIONS = {
    "billing_amount": {
        "name": "Billing Amount",
        "definition": "Total charge for medical services rendered during a patient's hospital stay. Must always be a positive number. Negative values indicate data entry errors or system bugs that should be caught by quality checks before reaching downstream marts.",
    },
    "admission_date": {
        "name": "Admission Date",
        "definition": "Date when the patient was admitted to the hospital. Must always precede the discharge date. Records where admission is after discharge indicate timestamp ordering bugs in the source system.",
    },
    "length_of_stay": {
        "name": "Length of Stay",
        "definition": "Calculated field: discharge_date minus admission_date, in days. Depends on both dates being valid. If admission and discharge dates are swapped, this value becomes negative — a clear quality signal.",
    },
    "denial_reason_code": {
        "name": "Denial Reason Code",
        "definition": "Enumerated reason a claim was denied (Sprint 1, LLD §1.2). Three codes: "
        "INVALID_BILLING_AMOUNT (billing_amount < 0 on the source claim — the seeded anomaly this "
        "demo detects), HIGH_RISK_SCORE (toy model's risk_score above threshold — background realism), "
        "RANDOM_AUDIT (small baseline rate of unrelated denials — background realism, keeps the dataset "
        "from looking artificially clean).",
    },
    "denial_risk_score": {
        "name": "Denial Risk Score",
        "definition": "0-1 score from the toy denial-risk heuristic (model_version 'toy-denial-risk-v1', "
        "LLD §2): 0.6 x the claim's (insurance_provider, medical_condition) segment denial rate, plus "
        "0.4 x a normalized billing-amount z-score within that segment. A deterministic, explicitly "
        "untuned heuristic — not a fitted model.",
    },
}

GLOSSARY_ASSIGNMENTS = {
    "billing_amount": ["raw_patients", "mart_billing"],
    "admission_date": ["raw_patients", "staging_patients"],
    "length_of_stay": ["mart_billing"],
    "denial_reason_code": ["denials"],
    "denial_risk_score": ["denial_model_scores"],
}

# ─── Ownership ───
OWNERSHIP_ASSIGNMENTS = {
    "clinical_team": ["raw_patients", "staging_patients"],
    "finance_team": ["mart_billing"],
    "research_team": ["mart_demographics"],
    # New group for Sprint 1's claims layer — distinct from the existing
    # clinical/finance/research teams, matching US-1's "claims operations
    # lead" persona directly (LLD §4 step 3).
    "claims_ops_team": ["claims", "denials", "denial_model_scores"],
}


# ─── URN discovery ───
def discover_urns(graph, platform_instance):
    query = f"""
    {{ search(input: {{type: DATASET, query: "{platform_instance}", start: 0, count: 100}}) {{
        searchResults {{ entity {{ urn ... on Dataset {{ name platform {{ name }} }} }} }}
    }} }}
    """
    result = graph.execute_graphql(query)
    urn_map = {}
    for item in result.get("search", {}).get("searchResults", []):
        entity = item.get("entity", {})
        if entity.get("platform", {}).get("name", "") != PLATFORM:
            continue
        if f",{platform_instance}." not in entity.get("urn", ""):
            continue
        name = entity.get("name", "")
        simple = name.split(".")[-1] if "." in name else name
        urn_map[simple] = entity["urn"]
    return urn_map


# ─── Emission (batch pattern: collect all per table, emit once) ───
def create_tags(emitter):
    for name, info in TAG_DEFINITIONS.items():
        emitter.emit(MetadataChangeProposalWrapper(
            entityUrn=f"urn:li:tag:{name}",
            aspect=TagPropertiesClass(name=name, description=info["description"]),
        ))
        print(f"    ✓ Created tag: {name}")


def attach_tags(emitter, urn_map):
    table_tags = {}
    for tag, tables in TAG_ASSIGNMENTS.items():
        for t in tables:
            if t not in urn_map:
                continue
            if t not in table_tags:
                table_tags[t] = []
            table_tags[t].append(f"urn:li:tag:{tag}")

    count = 0
    for table, tag_urns in table_tags.items():
        emitter.emit(MetadataChangeProposalWrapper(
            entityUrn=urn_map[table],
            aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=u) for u in tag_urns]),
        ))
        names = [u.split(":")[-1] for u in tag_urns]
        print(f"    ✓ {table} ← tags: {', '.join(names)}")
        count += 1
    return count


def create_glossary(emitter):
    for key, info in GLOSSARY_DEFINITIONS.items():
        emitter.emit(MetadataChangeProposalWrapper(
            entityUrn=f"urn:li:glossaryTerm:{key}",
            aspect=GlossaryTermInfoClass(name=info["name"], definition=info["definition"], termSource="INTERNAL"),
        ))
        print(f"    ✓ Created term: {info['name']}")


def attach_glossary(emitter, urn_map):
    now_ms = int(time.time() * 1000)
    table_terms = {}
    for key, tables in GLOSSARY_ASSIGNMENTS.items():
        for t in tables:
            if t not in urn_map:
                continue
            if t not in table_terms:
                table_terms[t] = []
            table_terms[t].append(f"urn:li:glossaryTerm:{key}")

    count = 0
    for table, term_urns in table_terms.items():
        emitter.emit(MetadataChangeProposalWrapper(
            entityUrn=urn_map[table],
            aspect=GlossaryTermsClass(
                terms=[GlossaryTermAssociationClass(urn=u) for u in term_urns],
                auditStamp=AuditStampClass(time=now_ms, actor="urn:li:corpuser:datahub"),
            ),
        ))
        names = [u.split(":")[-1] for u in term_urns]
        print(f"    ✓ {table} ← terms: {', '.join(names)}")
        count += 1
    return count


def emit_ownership(emitter, urn_map):
    count = 0
    for owner, tables in OWNERSHIP_ASSIGNMENTS.items():
        for t in tables:
            if t not in urn_map:
                continue
            emitter.emit(MetadataChangeProposalWrapper(
                entityUrn=urn_map[t],
                aspect=OwnershipClass(owners=[
                    OwnerClass(owner=f"urn:li:corpGroup:{owner}", type=OwnershipTypeClass.DATAOWNER)
                ]),
            ))
            print(f"    ✓ owner:{owner} → {t}")
            count += 1
    return count


def main():
    dry_run = "--dry-run" in sys.argv

    if not DATAHUB_TOKEN:
        print(
            "  ✗ DATAHUB_GMS_TOKEN not set. This DataHub instance requires "
            "auth (METADATA_SERVICE_AUTH_ENABLED=true), so anonymous requests "
            "get a 401. Generate a Personal Access Token in the DataHub UI "
            "and add it to .env -- see src/datahub/README.md#authentication."
        )
        sys.exit(1)

    print(f"Connecting to DataHub at {DATAHUB_SERVER}...")
    try:
        graph = DataHubGraph(DatahubClientConfig(server=DATAHUB_SERVER, token=DATAHUB_TOKEN))
    except Exception as e:
        print(f"  ✗ Cannot connect: {e}")
        sys.exit(1)

    emitter = DatahubRestEmitter(DATAHUB_SERVER, token=DATAHUB_TOKEN)

    urn_map = discover_urns(graph, DEFAULT_INSTANCE)
    if not urn_map:
        print(f"  No datasets found for '{DEFAULT_INSTANCE}'. Run ingestion first.")
        sys.exit(1)

    print(f"  Found {len(urn_map)} datasets")

    if dry_run:
        for name in sorted(urn_map.keys()):
            print(f"    {name}: {urn_map[name]}")
        return

    print(f"\n  Creating tags...")
    create_tags(emitter)
    print(f"\n  Creating glossary terms...")
    create_glossary(emitter)

    print(f"\n  Attaching tags...")
    attach_tags(emitter, urn_map)
    print(f"\n  Attaching glossary terms...")
    attach_glossary(emitter, urn_map)
    print(f"\n  Adding ownership...")
    emit_ownership(emitter, urn_map)

    print(f"\n{'='*50}")
    print(f"✅ Metadata complete")
    print(f"   Tags: pii, critical, internal, quality_monitored, pipeline_stage")
    print(f"   Glossary: Billing Amount, Admission Date, Length of Stay, Denial Reason Code, Denial Risk Score")
    print(f"   Owners: clinical_team, finance_team, research_team, claims_ops_team")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()