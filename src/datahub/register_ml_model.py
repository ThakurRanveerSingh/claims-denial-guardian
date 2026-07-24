#!/usr/bin/env python3
"""
Register the toy denial-risk scorer as a real MLModel entity in DataHub,
with MLFeature/MLFeatureTable lineage back to claims and forward to
denial_model_scores.

UAT gap (docs/walkthroughs/sprint-1-day1.md): Sprint 1 only registered
denial_model_scores as a plain Dataset -- a simplification the LLD itself
flagged (§4) as worth revisiting if there's schedule slack. US-6 and the
Production ML judging track now require a real MLModel entity.

Model note: denial_risk_model is a deterministic weighted heuristic
(score_claims.py, LLD §2) -- NOT a trained logistic regression model.
Registered here as what it actually is, not what it sounds like it should be.

Run AFTER Sprint 1's ingestion/lineage/metadata steps (claims and
denial_model_scores must already be registered):
    datahub ingest -c ingest.yaml
    python add_lineage.py
    python add_metadata.py
    python register_ml_model.py

Supports: --dry-run
"""

import os
import sys

from dotenv import load_dotenv

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    BaseDataClass,
    MLFeatureDataTypeClass,
    MLFeaturePropertiesClass,
    MLFeatureTablePropertiesClass,
    MLModelPropertiesClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    TrainingDataClass,
)
from datahub.metadata.urns import MlFeatureTableUrn, MlFeatureUrn, MlModelUrn
from datahub.specific.dataset import DatasetPatchBuilder

# See add_lineage.py for why this is required now (docs/decisions/0003).
load_dotenv()

DATAHUB_SERVER = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
DATAHUB_TOKEN = os.environ.get("DATAHUB_GMS_TOKEN")
SQLITE_PLATFORM = "sqlite"
DEFAULT_INSTANCE = "healthcare"

# The model isn't tied to any ML framework -- it's a plain Python heuristic
# (LLD §2), so "python" is the honest platform label here, not something
# like "mlflow" that isn't actually in use in this project.
ML_PLATFORM = f"urn:li:dataPlatform:python"

MODEL_NAME = "denial_risk_model"
FEATURE_TABLE_NAME = "denial_risk_features"
FEATURE_NAMESPACE = FEATURE_TABLE_NAME

# Each feature's real source columns on `claims` -- confirmed to exist via a
# live DataHub schema query (fetch_claims_schema_fields) before any of this
# is emitted, not assumed blindly.
FEATURE_DEFINITIONS = {
    "segment_denial_rate": {
        "description": (
            "Denial rate within the claim's (insurance_provider, medical_condition) segment. "
            "Computed from the same denials this sprint generates -- a backfill/demo scorer, "
            "not a train/score split (LLD §2's stated circularity)."
        ),
        "data_type": MLFeatureDataTypeClass.CONTINUOUS,
        "source_columns": ["insurance_provider", "medical_condition"],
    },
    "billing_zscore": {
        "description": "Z-score of the claim's billing_amount within its (insurance_provider, medical_condition) segment.",
        "data_type": MLFeatureDataTypeClass.CONTINUOUS,
        "source_columns": ["billing_amount"],
    },
}


# Same discovery pattern as add_lineage.py / add_metadata.py -- copied
# rather than imported, matching how those two scripts already duplicate it
# rather than sharing a module.
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
        if entity.get("platform", {}).get("name", "") != SQLITE_PLATFORM:
            continue
        if f",{platform_instance}." not in entity.get("urn", ""):
            continue
        name = entity.get("name", "")
        simple = name.split(".")[-1] if "." in name else name
        urn_map[simple] = entity["urn"]
    return urn_map


def fetch_claims_schema_fields(graph, claims_urn):
    """Read claims' actual registered schema from DataHub -- feature sources
    must point at real, live fields, not assumed column names."""
    query = """
    query($urn: String!) {
      dataset(urn: $urn) { schemaMetadata { fields { fieldPath } } }
    }
    """
    result = graph.execute_graphql(query, variables={"urn": claims_urn})
    schema = result.get("dataset", {}).get("schemaMetadata")
    if not schema:
        print(f"  ✗ Could not read claims schema from DataHub (urn: {claims_urn})")
        sys.exit(1)
    return {f["fieldPath"] for f in schema["fields"]}


def build_feature_mcps(claims_urn, claims_fields):
    mcps = []
    feature_urns = []
    for feature_name, defn in FEATURE_DEFINITIONS.items():
        # Each feature's source_columns are still validated against claims'
        # real schema (below) -- but MLFeature.sources only accepts
        # dataset-level entity references, not column-level schemaField
        # URNs. Confirmed against the live GMS: a schemaField URN there
        # gets rejected with a 422 ("not a valid destination for field
        # path: /sources/*"). Column names live in the description text
        # instead, since that's the only place this granularity can go.
        missing = [c for c in defn["source_columns"] if c not in claims_fields]
        if missing:
            print(f"  ✗ {feature_name}: claims is missing columns {missing} -- aborting")
            sys.exit(1)

        feature_urn = str(MlFeatureUrn(feature_namespace=FEATURE_NAMESPACE, name=feature_name))
        feature_urns.append(feature_urn)

        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=feature_urn,
                aspect=MLFeaturePropertiesClass(
                    description=defn["description"],
                    dataType=defn["data_type"],
                    sources=[claims_urn],
                ),
            )
        )
    return mcps, feature_urns


def build_feature_table_mcp(feature_urns):
    table_urn = str(MlFeatureTableUrn(platform=ML_PLATFORM, name=FEATURE_TABLE_NAME))
    mcp = MetadataChangeProposalWrapper(
        entityUrn=table_urn,
        aspect=MLFeatureTablePropertiesClass(
            description=(
                "Input features for denial_risk_model, computed per "
                "(insurance_provider, medical_condition) segment (LLD §2)."
            ),
            mlFeatures=feature_urns,
        ),
    )
    return mcp, table_urn


def build_model_mcps(claims_urn, denial_model_scores_urn, feature_urns):
    model_urn = str(MlModelUrn(platform=ML_PLATFORM, name=MODEL_NAME, env="PROD"))

    properties = MLModelPropertiesClass(
        description=(
            "Toy denial-risk scorer (model_version 'toy-denial-risk-v1'). NOT a trained model -- "
            "a deterministic, explicitly untuned weighted-sum heuristic: risk_score = clamp01("
            "0.6 x segment_denial_rate + 0.4 x min(|billing_zscore| / 4, 1.0)). "
            "See docs/architecture/lld-sprint1.md §2 and src/datahub/score_claims.py."
        ),
        type="Rule-based heuristic (weighted sum, not a fitted model)",
        mlFeatures=feature_urns,
        customProperties={
            "model_version": "toy-denial-risk-v1",
            "denial_rate_weight": "0.6",
            "billing_zscore_weight": "0.4",
            "billing_zscore_cap": "4.0",
            # No tested mechanism in this DataHub version makes MLModel a
            # graph-traversable upstream of a Dataset (both UpstreamLineageClass
            # and the updateLineage GraphQL mutation reject non-Dataset URNs,
            # confirmed against the live GMS) -- documented here instead of a
            # lineage edge. See docs/walkthroughs/sprint-1-day1.md.
            "output_dataset": denial_model_scores_urn,
        },
    )

    training_data = TrainingDataClass(
        trainingData=[
            BaseDataClass(
                dataset=claims_urn,
                motivation=(
                    "segment_denial_rate is computed from the same denials this sprint generates "
                    "against these claims -- a backfill/demo scorer, not a model trained on separate "
                    "historical data (LLD §2's stated circularity)."
                ),
            )
        ]
    )

    ownership = OwnershipClass(
        owners=[OwnerClass(owner="urn:li:corpGroup:claims_ops_team", type=OwnershipTypeClass.DATAOWNER)]
    )

    mcps = [
        MetadataChangeProposalWrapper(entityUrn=model_urn, aspect=properties),
        MetadataChangeProposalWrapper(entityUrn=model_urn, aspect=training_data),
        MetadataChangeProposalWrapper(entityUrn=model_urn, aspect=ownership),
    ]
    return mcps, model_urn


def build_output_reference_mcps(denial_model_scores_urn, model_urn):
    """Document model -> denial_model_scores, since it can't be a real
    lineage edge in this DataHub version: both UpstreamLineageClass
    (UpstreamClass.dataset is strictly typed as DatasetUrn server-side) and
    the updateLineage GraphQL mutation ("Tried to add lineage edge with
    non-dataset node when we expect a dataset") reject an MLModel urn,
    confirmed directly against the live GMS. A DataJob detour doesn't
    actually close the gap either -- DataJobInputOutputClass only accepts
    Dataset urns too, so the model still wouldn't be a node in the graph.
    Recording the relationship as a custom property on denial_model_scores
    (the model's own customProperties already carries the matching
    "output_dataset" reference -- see build_model_mcps) instead of silently
    dropping it.
    """
    patch_builder = DatasetPatchBuilder(denial_model_scores_urn)
    patch_builder.add_custom_property("produced_by_model", model_urn)
    return list(patch_builder.build())


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

    urn_map = discover_urns(graph, DEFAULT_INSTANCE)
    if "claims" not in urn_map or "denial_model_scores" not in urn_map:
        print("  ✗ claims/denial_model_scores not found in DataHub -- run Sprint 1 ingestion first")
        sys.exit(1)
    claims_urn = urn_map["claims"]
    denial_model_scores_urn = urn_map["denial_model_scores"]

    claims_fields = fetch_claims_schema_fields(graph, claims_urn)
    print(f"  Read claims schema from DataHub: {len(claims_fields)} fields")

    feature_mcps, feature_urns = build_feature_mcps(claims_urn, claims_fields)
    table_mcp, table_urn = build_feature_table_mcp(feature_urns)
    model_mcps, model_urn = build_model_mcps(claims_urn, denial_model_scores_urn, feature_urns)
    output_reference_mcps = build_output_reference_mcps(denial_model_scores_urn, model_urn)

    all_mcps = feature_mcps + [table_mcp] + model_mcps + output_reference_mcps

    if dry_run:
        print(f"\nDRY RUN -- would emit {len(all_mcps)} MCPs:")
        print(f"  {len(feature_urns)} MLFeature(s): {feature_urns}")
        print(f"  1 MLFeatureTable: {table_urn}")
        print(f"  1 MLModel: {model_urn} (properties + trainingData + ownership)")
        print(f"  1 customProperty patch on {denial_model_scores_urn}")
        print(f"    (produced_by_model -> {model_urn}; not a lineage edge, see comment in code)")
        return

    emitter = DatahubRestEmitter(DATAHUB_SERVER, token=DATAHUB_TOKEN)
    for mcp in all_mcps:
        emitter.emit(mcp)

    print(f"\n✅ Registered {len(feature_urns)} MLFeature(s), 1 MLFeatureTable, 1 MLModel")
    print(f"   MLModel: {model_urn}")
    print(f"   Real lineage edges: claims -> {FEATURE_TABLE_NAME} -> {MODEL_NAME} (via mlFeatures)")
    print(f"                       claims -> {MODEL_NAME} (trainingData)")
    print(f"   Documented only (not a graph edge): {MODEL_NAME} -> denial_model_scores (customProperties)")


if __name__ == "__main__":
    main()
