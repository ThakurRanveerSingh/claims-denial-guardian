#!/usr/bin/env python3
"""
Score every claim with the toy denial-risk heuristic (LLD §2).

Run from src/datahub/, AFTER generate_denials.py (denials must already exist
so segment_denial_rate can be computed). Full correct rebuild sequence
(lld-sprint2.md §10.7 — see generate_denials.py's docstring for the
cumulative-mutation gotcha this ordering avoids):
    python seed_upstream_scenario.py       # only if using the second scenario (decision 0006)
    sqlite3 healthcare.db < schema_sprint1.sql
    python generate_denials.py
    python score_claims.py

Formula:
    segment_denial_rate = denied claims in (provider, condition) / total claims in (provider, condition)
    billing_zscore       = (claim.billing_amount - segment_mean_billing) / segment_stddev_billing
    risk_score           = clamp01(0.6 * segment_denial_rate + 0.4 * min(|billing_zscore| / 4, 1.0))

Deterministic heuristic, not a trained model (LLD §2): a "toy" framing with
an untuned 0.6/0.4 split, stated as such rather than dressed up as fitted.
Every claim gets scored, not just denied ones — the model predicts risk,
it doesn't just describe what already happened.
"""

import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Resolved via __file__, not the caller's cwd (Item 2 fix, this session) —
# same reasoning as generate_denials.py's DB_PATH: a bare "healthcare.db"
# resolves against whatever directory the script is RUN from, and sqlite3
# silently creates an empty file there instead of erroring, which is exactly
# the confusing failure mode this fix removes.
DB_PATH = Path(__file__).resolve().parent / "healthcare.db"
MODEL_VERSION = "toy-denial-risk-v1"

DENIAL_RATE_WEIGHT = 0.6
BILLING_ZSCORE_WEIGHT = 0.4
BILLING_ZSCORE_CAP = 4.0  # |zscore| / CAP is clamped to 1.0 before weighting


def clamp01(x):
    return max(0.0, min(1.0, x))


def load_claims(conn):
    return conn.execute(
        "SELECT claim_id, insurance_provider, medical_condition, billing_amount FROM claims ORDER BY claim_id"
    ).fetchall()


def load_denied_claim_ids(conn):
    return {row[0] for row in conn.execute("SELECT DISTINCT claim_id FROM denials").fetchall()}


def compute_segment_stats(claims, denied_ids):
    """One pass over claims to build per-(provider, condition) denial rate, mean, stddev.

    Same segment definition as generate_denials.py's spike — (insurance_provider,
    medical_condition) — so the model's risk story lines up with the anomaly it's
    meant to detect (LLD §2).
    """
    by_segment = defaultdict(list)
    for claim_id, provider, condition, billing_amount in claims:
        by_segment[(provider, condition)].append((claim_id, billing_amount))

    stats = {}
    for segment, rows in by_segment.items():
        amounts = [amount for _, amount in rows]
        denied_count = sum(1 for claim_id, _ in rows if claim_id in denied_ids)
        stats[segment] = {
            "denial_rate": denied_count / len(rows),
            "mean_billing": statistics.mean(amounts),
            "stddev_billing": statistics.pstdev(amounts),
        }
    return stats


def score_claim(billing_amount, segment_stats):
    denial_rate = segment_stats["denial_rate"]
    mean = segment_stats["mean_billing"]
    stddev = segment_stats["stddev_billing"]

    billing_zscore = (billing_amount - mean) / stddev
    normalized_zscore = min(abs(billing_zscore) / BILLING_ZSCORE_CAP, 1.0)
    risk_score = clamp01(DENIAL_RATE_WEIGHT * denial_rate + BILLING_ZSCORE_WEIGHT * normalized_zscore)
    return risk_score, billing_zscore


def main():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} does not exist.")
        print("This script expects claims/denials to already be populated. Run first:")
        print("    cd src/datahub/")
        print("    sqlite3 healthcare.db < schema_sprint1.sql")
        print("    python generate_denials.py")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM denial_model_scores")  # rerunning this script alone stays idempotent

    claims = load_claims(conn)
    denied_ids = load_denied_claim_ids(conn)
    segment_stats = compute_segment_stats(claims, denied_ids)

    scored_at = datetime.now(timezone.utc).isoformat()
    rows_to_insert = []
    for claim_id, provider, condition, billing_amount in claims:
        stats = segment_stats[(provider, condition)]
        risk_score, billing_zscore = score_claim(billing_amount, stats)
        rows_to_insert.append(
            (claim_id, MODEL_VERSION, risk_score, stats["denial_rate"], billing_zscore, scored_at)
        )

    conn.executemany(
        """
        INSERT INTO denial_model_scores
            (claim_id, model_version, risk_score, segment_denial_rate_feature, billing_zscore_feature, scored_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    conn.commit()

    # No fixed "high risk" cutoff is decided in Sprint 1 (thresholding is
    # Sentinel's job, deferred per LLD §6) — reporting the top of the
    # distribution instead of an arbitrary threshold count.
    top5 = sorted(rows_to_insert, key=lambda r: r[2], reverse=True)[:5]
    print(f"Scored {len(rows_to_insert)} claims with {MODEL_VERSION}")
    print(f"  risk_score range: {min(r[2] for r in rows_to_insert):.4f} .. {max(r[2] for r in rows_to_insert):.4f}")
    print(f"  top 5 by risk_score:")
    for claim_id, _, risk_score, denial_rate_feat, zscore_feat, _ in top5:
        print(f"    {claim_id}  risk={risk_score:.4f}  segment_denial_rate={denial_rate_feat:.4f}  billing_zscore={zscore_feat:.2f}")

    conn.close()


if __name__ == "__main__":
    main()
