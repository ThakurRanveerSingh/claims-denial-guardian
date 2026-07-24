#!/usr/bin/env python3
"""
Seed a SECOND anomaly, upstream of claims this time: docs/architecture/lld-sprint2.md
§10, docs/decisions/0006-two-scenario-seeding.md.

This is the contrast case to generate_denials.py's seed_segment_spike(). That
scenario (UnitedHealthcare/diabetes) mutates claims.billing_amount directly,
AFTER claims is already populated — so when Investigator later checks "does
this reproduce upstream?", the answer is mostly no (90% of those flagged
claims are clean all the way back to raw_patients; only 10% happen to overlap
the dataset's separate, pre-existing baseline defect rate). That's a real,
useful finding, but it only exercises one direction of Investigator's
hop-by-hop logic.

This script seeds the OTHER direction: a segment (Cigna/obesity) where the
negative billing_amount is written into raw_patients, staging_patients, AND
mart_billing — the same value, at the same rowid, in all three — BEFORE
claims is (re)built from them. Verified in lld-sprint2.md §10.2 that
raw_patients.rowid / staging_patients.rowid / mart_billing.rowid all refer to
the same logical row for all 55,500 rows (none of the three CREATE TABLE ...
AS SELECT statements in create_db.py filter or reorder), which is what makes
"write the same value at the same rowid into all three" well-defined. Once
mart_billing.billing_amount is negative for these rows, schema_sprint1.sql's
unmodified INSERT INTO claims ... carries it into claims automatically, and
generate_denials.py's existing rule 1 (deny_negative_billing) denies it with
the same INVALID_BILLING_AMOUNT reason code — no changes needed to either of
those scripts (decision 0006).

MUST run before schema_sprint1.sql (see lld-sprint2.md §10.7): schema_sprint1.sql
unconditionally DROPs and rebuilds claims from whatever is currently in
mart_billing, so if this script ran after, its edits would sit unused in
mart_billing until the next rebuild.

Full sequence, run from src/datahub/:
    python seed_upstream_scenario.py             # this script
    sqlite3 healthcare.db < schema_sprint1.sql
    python generate_denials.py
    python score_claims.py
"""

import random
import sqlite3

DB_PATH = "healthcare.db"

# Distinct from generate_denials.py's RANDOM_SEED = 42 — a different,
# deliberately-chosen value rather than a reused one, so the two scenarios'
# row selections are visibly independent, separately-tunable parameters, not
# an accidental copy-paste (decision 0006, "alternatives considered").
UPSTREAM_SEED = 43

# --- Implementation-time parameters (same "named, not buried" convention as
# generate_denials.py's SPIKE_SEGMENT/SPIKE_TARGET_RATE). ---
#
# NOTE on casing: this segment is selected against raw_patients, which still
# has the CSV's original casing ("Cigna", "Obesity") — NOT the lowercased
# form (condition_clean / mart_demographics.medical_condition / claims.medical_condition
# all lowercase it). mart_billing has no medical_condition column at all
# (dropped at the raw->staging->mart_billing fork in create_db.py), so the
# segment can only be defined against raw_patients/staging_patients, and the
# matching rowids are then reused, unchanged, to update mart_billing.
UPSTREAM_SEGMENT = ("Cigna", "Obesity")
UPSTREAM_TARGET_RATE = 0.15  # push this segment's negative rate to ~15% (distinct from the existing scenario's 20%)


def seed_upstream_scenario(conn, rng):
    """Flip a subset of UPSTREAM_SEGMENT's positive billing_amounts to negative,
    writing the same value at the same rowid into raw_patients, staging_patients,
    and mart_billing together.

    Target-based, same idempotency pattern as generate_denials.py's
    seed_segment_spike(): re-running this script without first rebuilding
    raw_patients from the source CSV is a no-op once the target rate is
    already met, rather than repeatedly over-flipping on every rerun.
    """
    provider, condition = UPSTREAM_SEGMENT

    # Segment membership + current sign is read from raw_patients (the only
    # one of the three tables that still carries medical_condition). rowid
    # is what carries this selection over to staging_patients/mart_billing —
    # not name/hospital or any other join key, per the verified rowid
    # alignment in lld-sprint2.md §10.2.
    rows = conn.execute(
        """
        SELECT rowid, CAST(billing_amount AS REAL) FROM raw_patients
        WHERE insurance_provider = ? AND medical_condition = ?
        ORDER BY rowid
        """,
        (provider, condition),
    ).fetchall()

    total = len(rows)
    already_negative = [r for r in rows if r[1] < 0]
    positive = [r for r in rows if r[1] >= 0]
    target_negative_count = round(total * UPSTREAM_TARGET_RATE)
    need_to_flip = max(0, target_negative_count - len(already_negative))

    to_flip = rng.sample(positive, min(need_to_flip, len(positive)))
    rowids = [(rowid,) for rowid, _ in to_flip]

    # raw_patients and staging_patients both store billing_amount as TEXT
    # (create_db.py's load_csv() loads every raw_patients column as TEXT, and
    # staging_patients's `SELECT *, <_clean columns> FROM raw_patients` passes
    # billing_amount through completely unchanged — no cast). Negate it with
    # the exact same SQL idiom create_db.py's own plant_quality_issues() uses
    # for the dataset's baseline negative-billing defect
    # (CAST(-1 * ABS(CAST(billing_amount AS REAL)) AS TEXT)), so these
    # injected values are textually indistinguishable from the pre-existing
    # ones — not a special case, the same defect shape, just concentrated in
    # one segment instead of spread uniformly at random.
    conn.executemany(
        """
        UPDATE raw_patients
        SET billing_amount = CAST(-1 * ABS(CAST(billing_amount AS REAL)) AS TEXT)
        WHERE rowid = ?
        """,
        rowids,
    )
    conn.executemany(
        """
        UPDATE staging_patients
        SET billing_amount = CAST(-1 * ABS(CAST(billing_amount AS REAL)) AS TEXT)
        WHERE rowid = ?
        """,
        rowids,
    )
    # mart_billing already stores billing_amount as REAL (cast once, at
    # mart-build time, in create_db.py's create_mart_tables()) — a plain
    # numeric negation, no text formatting needed.
    conn.executemany(
        "UPDATE mart_billing SET billing_amount = -ABS(billing_amount) WHERE rowid = ?",
        rowids,
    )

    return len(to_flip), target_negative_count, total


def main():
    conn = sqlite3.connect(DB_PATH)
    rng = random.Random(UPSTREAM_SEED)

    flipped, target, total = seed_upstream_scenario(conn, rng)
    conn.commit()
    conn.close()

    provider, condition = UPSTREAM_SEGMENT
    print(
        f"Seeded upstream anomaly in ({provider}, {condition}): flipped {flipped} rows to "
        f"negative billing_amount in raw_patients + staging_patients + mart_billing "
        f"(target ~{target}/{total} ≈ {UPSTREAM_TARGET_RATE:.0%})"
    )
    print(
        "Next: sqlite3 healthcare.db < schema_sprint1.sql   "
        "(claims must be rebuilt from the now-modified mart_billing before this takes effect)"
    )


if __name__ == "__main__":
    main()
