"""
Checks that denial_risk_model and its supporting MLFeatureTable/MLFeatures
are actually registered in DataHub -- the UAT gap documented in
docs/walkthroughs/sprint-1-day1.md (no MLModel entity existed at all) can't
silently regress.

Requires a live DataHub instance at localhost:8080 (same as
register_ml_model.py). Skips rather than fails if DataHub isn't reachable --
this checks external system state, not local code, so "DataHub is down" and
"the registration regressed" need to stay distinguishable.
"""

import pytest
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

DATAHUB_SERVER = "http://localhost:8080"

MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:python,denial_risk_model,PROD)"
FEATURE_TABLE_URN = "urn:li:mlFeatureTable:(urn:li:dataPlatform:python,denial_risk_features)"
FEATURE_URNS = {
    "urn:li:mlFeature:(denial_risk_features,segment_denial_rate)",
    "urn:li:mlFeature:(denial_risk_features,billing_zscore)",
}
CLAIMS_URN = "urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.claims,PROD)"
DENIAL_MODEL_SCORES_URN = "urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.denial_model_scores,PROD)"


@pytest.fixture(scope="module")
def graph():
    g = DataHubGraph(DatahubClientConfig(server=DATAHUB_SERVER))
    try:
        g.execute_graphql("{ __typename }")
    except Exception as e:
        pytest.skip(f"DataHub not reachable at {DATAHUB_SERVER}: {e}")
    return g


def _entity_exists(graph, urn):
    result = graph.execute_graphql(
        "query($urn: String!) { entity(urn: $urn) { urn } }",
        variables={"urn": urn},
    )
    return result.get("entity") is not None


def test_ml_model_entity_exists(graph):
    assert _entity_exists(graph, MODEL_URN)


def test_ml_feature_table_entity_exists(graph):
    assert _entity_exists(graph, FEATURE_TABLE_URN)


def test_ml_features_exist(graph):
    for urn in FEATURE_URNS:
        assert _entity_exists(graph, urn)


def test_model_properties_describe_a_heuristic_not_logistic_regression(graph):
    """Regression guard for an earlier premise-correction this session: the
    model is a deterministic weighted heuristic, not logistic regression.
    If the description ever gets "fixed" to say otherwise without actually
    changing score_claims.py, that's false metadata -- exactly what this
    catches."""
    result = graph.execute_graphql(
        "query($urn: String!) { mlModel(urn: $urn) { properties { description type } } }",
        variables={"urn": MODEL_URN},
    )
    props = result["mlModel"]["properties"]
    assert "logistic regression" not in props["description"].lower()
    assert "logistic regression" not in (props["type"] or "").lower()
    assert "heuristic" in props["description"].lower()


def test_model_has_ownership(graph):
    result = graph.execute_graphql(
        """query($urn: String!) {
             mlModel(urn: $urn) { ownership { owners { owner { ... on CorpGroup { urn } } } } }
           }""",
        variables={"urn": MODEL_URN},
    )
    owners = {o["owner"]["urn"] for o in result["mlModel"]["ownership"]["owners"]}
    assert "urn:li:corpGroup:claims_ops_team" in owners


def test_model_links_to_its_features(graph):
    result = graph.execute_graphql(
        "query($urn: String!) { mlModel(urn: $urn) { properties { mlFeatures } } }",
        variables={"urn": MODEL_URN},
    )
    assert set(result["mlModel"]["properties"]["mlFeatures"]) == FEATURE_URNS


def test_feature_table_links_to_same_features(graph):
    result = graph.execute_graphql(
        "query($urn: String!) { mlFeatureTable(urn: $urn) { properties { mlFeatures { urn } } } }",
        variables={"urn": FEATURE_TABLE_URN},
    )
    linked = {f["urn"] for f in result["mlFeatureTable"]["properties"]["mlFeatures"]}
    assert linked == FEATURE_URNS


def test_denial_model_scores_still_has_original_claims_lineage(graph):
    """Regression guard: register_ml_model.py must never touch
    UpstreamLineageClass (it can't add the model as a real upstream there --
    confirmed server-side rejection -- so it must not silently drop the
    existing claims edge from Sprint 1 either)."""
    result = graph.execute_graphql(
        """query($urn: String!) {
             dataset(urn: $urn) {
               lineage(input: {direction: UPSTREAM, start: 0, count: 10}) {
                 relationships { entity { urn } }
               }
             }
           }""",
        variables={"urn": DENIAL_MODEL_SCORES_URN},
    )
    upstream_urns = {r["entity"]["urn"] for r in result["dataset"]["lineage"]["relationships"]}
    assert CLAIMS_URN in upstream_urns


def test_denial_model_scores_documents_which_model_produced_it(graph):
    """The model -> denial_model_scores hop isn't a real lineage edge in
    this DataHub version (see docs/walkthroughs/sprint-1-day1.md) -- this
    checks the documented customProperties fallback instead."""
    result = graph.execute_graphql(
        "query($urn: String!) { dataset(urn: $urn) { properties { customProperties { key value } } } }",
        variables={"urn": DENIAL_MODEL_SCORES_URN},
    )
    props = {p["key"]: p["value"] for p in result["dataset"]["properties"]["customProperties"]}
    assert props.get("produced_by_model") == MODEL_URN
