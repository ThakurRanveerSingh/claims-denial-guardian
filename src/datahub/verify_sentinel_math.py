#!/usr/bin/env python3
"""
Slice 0 proof script — does the real, regenerated data actually trip
Sentinel's detection math?

This is NOT src/agents/sentinel.py (that's Slice 1, not built yet). It's a
standalone check that the two-proportion z-test lld-sprint2.md §1.2 designs
Sentinel around actually produces the separation the design predicts, run
against the REAL claims/denials tables in the committed healthcare.db (after
seed_upstream_scenario.py + schema_sprint1.sql + generate_denials.py +
score_claims.py have all run — see docs/architecture/lld-sprint2.md §10.7).

Implements the exact formula from §1.2, leave-one-out baseline (the segment
under test is excluded from both the baseline rate and the standard-error
calculation, not folded into a whole-population comparison — see §1.2's
"why leave-one-out" for the reasoning: including the flagged segment in its
own baseline would dilute the very signal being measured):

    p_segment = segment_denials / segment_claims
    p_rest    = (total_denials - segment_denials) / (total_claims - segment_claims)
    p_pool    = (segment_denials + (total_denials - segment_denials)) / total_claims
    se        = sqrt(p_pool * (1 - p_pool) * (1/segment_claims + 1/(total_claims - segment_claims)))
    z         = (p_segment - p_rest) / se

Run from src/datahub/, after the full seed sequence:
    python verify_sentinel_math.py
"""

import math
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "healthcare.db"

# Matches lld-sprint2.md §1.2's default SENTINEL_Z_THRESHOLD.
Z_THRESHOLD = 3.5

# The two seeded segments this script exists to confirm — for the summary
# table's ordering/highlighting only, NOT used to special-case the math
# itself. The z-test below runs identically across all 30 segments; these
# two are just the ones with a known predicted outcome to check against.
SEEDED_SEGMENTS = {
    ("UnitedHealthcare", "diabetes"),  # existing scenario, generate_denials.py
    ("Cigna", "obesity"),              # new scenario, seed_upstream_scenario.py
}


def load_segment_counts(conn):
    """One row per (insurance_provider, medical_condition): claim count + denied count.

    Same segment definition as generate_denials.py/score_claims.py (LLD §1.1,
    reaffirmed lld-sprint2.md §1.1) — 30 segments.
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
    return {(provider, condition): (total, denied) for provider, condition, total, denied in rows}


def two_proportion_z_test(segment_claims, segment_denials, total_claims, total_denials):
    """§1.2's exact formula, leave-one-out. Returns (z, p_segment, p_rest)."""
    rest_claims = total_claims - segment_claims
    rest_denials = total_denials - segment_denials

    p_segment = segment_denials / segment_claims
    p_rest = rest_denials / rest_claims
    p_pool = (segment_denials + rest_denials) / total_claims

    se = math.sqrt(p_pool * (1 - p_pool) * (1 / segment_claims + 1 / rest_claims))
    z = (p_segment - p_rest) / se
    return z, p_segment, p_rest


def main():
    assert DB_PATH.exists(), f"healthcare.db not found at {DB_PATH}"
    conn = sqlite3.connect(DB_PATH)

    segment_counts = load_segment_counts(conn)
    conn.close()

    total_claims = sum(total for total, _ in segment_counts.values())
    total_denials = sum(denied for _, denied in segment_counts.values())
    assert len(segment_counts) == 30, f"expected 30 segments, found {len(segment_counts)}"

    results = []
    for segment, (total, denied) in segment_counts.items():
        z, p_segment, p_rest = two_proportion_z_test(total, denied, total_claims, total_denials)
        results.append(
            {
                "segment": segment,
                "n": total,
                "denied": denied,
                "rate": p_segment,
                "baseline": p_rest,
                "z": z,
                "flagged": z > Z_THRESHOLD,
            }
        )

    results.sort(key=lambda r: r["z"], reverse=True)

    print(f"Two-proportion z-test (leave-one-out baseline), Z_THRESHOLD = {Z_THRESHOLD}")
    print(f"Total: {total_claims} claims, {total_denials} denials across {len(results)} segments\n")

    header = f"{'Segment':<38} {'n':>6} {'denied':>7} {'rate':>8} {'baseline':>9} {'z':>9}  flagged"
    print(header)
    print("-" * len(header))
    for r in results:
        provider, condition = r["segment"]
        seeded_marker = "  <-- SEEDED" if r["segment"] in SEEDED_SEGMENTS else ""
        print(
            f"{provider + '/' + condition:<38} {r['n']:>6} {r['denied']:>7} "
            f"{r['rate']:>8.2%} {r['baseline']:>9.2%} {r['z']:>9.2f}  "
            f"{'YES' if r['flagged'] else 'no'}{seeded_marker}"
        )

    print()
    seeded_flagged = {r["segment"] for r in results if r["flagged"]} & SEEDED_SEGMENTS
    non_seeded_flagged = [r for r in results if r["flagged"] and r["segment"] not in SEEDED_SEGMENTS]
    seeded_missed = SEEDED_SEGMENTS - seeded_flagged

    if seeded_missed:
        print(f"WARNING: seeded segment(s) did NOT clear the threshold: {seeded_missed}")
    if non_seeded_flagged:
        print(f"WARNING: non-seeded segment(s) unexpectedly flagged: {[r['segment'] for r in non_seeded_flagged]}")
    if not seeded_missed and not non_seeded_flagged:
        print(
            f"OK: exactly the {len(SEEDED_SEGMENTS)} seeded segments cleared Z_THRESHOLD={Z_THRESHOLD}, "
            f"and no others did."
        )


if __name__ == "__main__":
    main()
