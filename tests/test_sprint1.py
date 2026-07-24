"""
Sprint 1 checks against the generated healthcare.db: schema, seeded anomaly,
and model scores actually landed as designed (docs/architecture/lld-sprint1.md).

Assumes the full pipeline has already been run:
    sqlite3 src/datahub/healthcare.db < src/datahub/schema_sprint1.sql
    python src/datahub/generate_denials.py
    python src/datahub/score_claims.py
"""

import sqlite3
from pathlib import Path

import pytest

DB_PATH = Path(__file__).parent.parent / "src" / "datahub" / "healthcare.db"

SPIKE_SEGMENT = ("UnitedHealthcare", "diabetes")  # must match generate_denials.py
# Second seeded anomaly (docs/decisions/0006-two-scenario-seeding.md): a
# genuinely upstream-caused incident, must match seed_upstream_scenario.py.
UPSTREAM_SEGMENT = ("Cigna", "obesity")
SEEDED_SEGMENTS = {SPIKE_SEGMENT, UPSTREAM_SEGMENT}
VALID_REASON_CODES = {"INVALID_BILLING_AMOUNT", "HIGH_RISK_SCORE", "RANDOM_AUDIT"}


@pytest.fixture(scope="module")
def conn():
    # sqlite3.connect() silently creates an empty file if DB_PATH is wrong,
    # which would otherwise surface as confusing "no such table" errors below
    # instead of a clear "the fixture isn't where you think it is."
    assert DB_PATH.exists(), (
        f"healthcare.db not found at {DB_PATH} — run schema_sprint1.sql, "
        "generate_denials.py, and score_claims.py first"
    )
    connection = sqlite3.connect(DB_PATH)
    yield connection
    connection.close()


# --- tables exist ---


def test_new_tables_exist(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"claims", "denials", "denial_model_scores"} <= tables


# --- row counts sane ---


def test_claims_row_count_matches_mart_billing(conn):
    claims_count = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    mart_billing_count = conn.execute("SELECT COUNT(*) FROM mart_billing").fetchone()[0]
    assert claims_count == mart_billing_count == 55500


def test_claims_have_no_null_medical_condition(conn):
    # medical_condition is joined in from mart_demographics via rowid (LLD §1.1) —
    # a NULL here would mean that join silently dropped or misaligned rows.
    null_count = conn.execute("SELECT COUNT(*) FROM claims WHERE medical_condition IS NULL").fetchone()[0]
    assert null_count == 0


def test_denials_row_count_is_sane(conn):
    total_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    total_denials = conn.execute("SELECT COUNT(*) FROM denials").fetchone()[0]
    assert 0 < total_denials < total_claims


def test_every_denial_references_a_real_claim(conn):
    orphans = conn.execute(
        "SELECT COUNT(*) FROM denials d LEFT JOIN claims c ON c.claim_id = d.claim_id WHERE c.claim_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_no_duplicate_denials_per_claim(conn):
    dupes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT claim_id FROM denials GROUP BY claim_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert dupes == 0


def test_denial_reason_codes_are_from_the_closed_set(conn):
    codes = {row[0] for row in conn.execute("SELECT DISTINCT denial_reason_code FROM denials")}
    assert codes <= VALID_REASON_CODES


def test_invalid_billing_denial_amount_matches_claim_billing_amount(conn):
    # Sprint 1 simplification (LLD §1.2): denial_amount always = claims.billing_amount.
    mismatches = conn.execute(
        """
        SELECT COUNT(*) FROM denials d
        JOIN claims c ON c.claim_id = d.claim_id
        WHERE d.denial_reason_code = 'INVALID_BILLING_AMOUNT'
          AND d.denial_amount != c.billing_amount
        """
    ).fetchone()[0]
    assert mismatches == 0


# --- anomaly present ---


def test_seeded_anomaly_segments_are_clear_outliers(conn):
    """LLD §3 (single scenario) + lld-sprint2.md §10 / decision 0006 (second,
    upstream-caused scenario added in Slice 0): the two seeded segments'
    denial rates should both be pushed well above every other segment's —
    the anomalies Sentinel is meant to detect and Investigator meant to
    trace, tracing to two different root causes (introduced-at-claims vs.
    inherited-from-raw_patients).

    Originally asserted the single top segment was > 2x the *second*-place
    rate — a "clear outlier from the pack" check. That assertion broke the
    moment a second real anomaly of comparable size existed by design
    (20.76% vs. 15.99% — nowhere near a 2x gap from each other, because
    both are genuine seeded incidents, not one signal plus noise). The
    correct check now compares both seeded segments against the *third*-place
    rate — the highest of the 28 untouched segments, i.e. the real
    background/noise ceiling — rather than against each other.
    """
    rows = conn.execute(
        """
        SELECT c.insurance_provider, c.medical_condition,
               COUNT(*) AS total,
               SUM(CASE WHEN d.claim_id IS NOT NULL THEN 1 ELSE 0 END) AS denied
        FROM claims c
        LEFT JOIN denials d ON d.claim_id = c.claim_id
        GROUP BY c.insurance_provider, c.medical_condition
        """
    ).fetchall()

    rates = sorted(
        ((provider, condition, denied / total) for provider, condition, total, denied in rows),
        key=lambda r: r[2],
        reverse=True,
    )

    top_two_segments = {(provider, condition) for provider, condition, _ in rates[:2]}
    top_two_rates = [rate for _, _, rate in rates[:2]]
    background_ceiling = rates[2][2]  # highest rate among the 28 untouched segments

    assert top_two_segments == SEEDED_SEGMENTS
    assert all(rate > 0.10 for rate in top_two_rates)  # well above the ~2-4% baseline elsewhere
    assert min(top_two_rates) > background_ceiling * 2  # BOTH seeded segments clear outliers from the untouched pack


# --- model produces scores ---


def test_every_claim_is_scored(conn):
    total_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    total_scores = conn.execute("SELECT COUNT(*) FROM denial_model_scores").fetchone()[0]
    assert total_scores == total_claims


def test_risk_scores_are_in_valid_range(conn):
    min_score, max_score = conn.execute("SELECT MIN(risk_score), MAX(risk_score) FROM denial_model_scores").fetchone()
    assert 0.0 <= min_score
    assert max_score <= 1.0


def test_negative_billing_claims_score_highest(conn):
    """LLD §2: the seeded negative-billing anomaly should rank as the
    highest-risk-scored claims in the whole dataset — the ranking story
    Investigator's narrative depends on (see docs/walkthroughs/sprint-1-day1.md
    for why the absolute ceiling is well under 1.0 given the untuned weights)."""
    top_scored_claim_ids = {
        row[0] for row in conn.execute("SELECT claim_id FROM denial_model_scores ORDER BY risk_score DESC LIMIT 5")
    }
    negative_billing_claim_ids = {
        row[0] for row in conn.execute("SELECT claim_id FROM claims WHERE billing_amount < 0")
    }
    assert top_scored_claim_ids <= negative_billing_claim_ids
