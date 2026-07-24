#!/usr/bin/env python3
"""
Generate the `denials` table content: LLD §3's denial rule + seeded anomaly.

Run AFTER schema_sprint1.sql (claims must already exist and be populated):
    sqlite3 healthcare.db < schema_sprint1.sql
    python generate_denials.py

Rule (docs/architecture/lld-sprint1.md §3):
  1. Every claim with billing_amount < 0 is denied, reason INVALID_BILLING_AMOUNT.
  2. A small random baseline is denied for RANDOM_AUDIT / HIGH_RISK_SCORE, so the
     dataset doesn't look artificially clean.
  3. One (insurance_provider, medical_condition) segment gets its negative-billing
     concentration pushed well above the ~2% baseline — the spike Sentinel is
     meant to detect and Investigator meant to trace.
"""

import random
import sqlite3

DB_PATH = "healthcare.db"

# Same seed convention as create_db.py — full reproducibility across reruns.
RANDOM_SEED = 42

# --- Implementation-time parameters (LLD §3: explicitly NOT an architecture
# decision — free to retune without revisiting the design). ---
SPIKE_SEGMENT = ("UnitedHealthcare", "diabetes")  # LLD §3's own suggested example
SPIKE_TARGET_RATE = 0.20                          # push this segment's negative rate to ~20%
RANDOM_AUDIT_RATE = 0.005                         # background baseline, of all claims
HIGH_RISK_SCORE_RATE = 0.005                      # background baseline, of all claims


def seed_segment_spike(conn, rng):
    """Flip a subset of SPIKE_SEGMENT's positive billing_amounts to negative.

    Target-based rather than a fixed flip-count: re-running this script (without
    re-applying schema_sprint1.sql first) is then a no-op once the target is
    reached, instead of over-flipping on every rerun.
    """
    provider, condition = SPIKE_SEGMENT
    rows = conn.execute(
        """
        SELECT claim_id, billing_amount FROM claims
        WHERE insurance_provider = ? AND medical_condition = ?
        ORDER BY claim_id
        """,
        (provider, condition),
    ).fetchall()

    total = len(rows)
    already_negative = [r for r in rows if r[1] < 0]
    positive = [r for r in rows if r[1] >= 0]
    target_negative_count = round(total * SPIKE_TARGET_RATE)
    need_to_flip = max(0, target_negative_count - len(already_negative))

    to_flip = rng.sample(positive, min(need_to_flip, len(positive)))
    conn.executemany(
        "UPDATE claims SET billing_amount = ? WHERE claim_id = ?",
        [(-abs(amount), claim_id) for claim_id, amount in to_flip],
    )
    return len(to_flip), target_negative_count, total


def deny_negative_billing(conn):
    """Rule 1: every negative-billing claim is denied, reason INVALID_BILLING_AMOUNT.

    denial_amount is set equal to billing_amount (including its sign) per the
    Sprint 1 simplification stated in LLD §1.2 — full denial only, no partial
    denials modeled.
    """
    rows = conn.execute(
        "SELECT claim_id, billing_amount, discharge_date FROM claims WHERE billing_amount < 0 ORDER BY claim_id"
    ).fetchall()
    conn.executemany(
        """
        INSERT INTO denials (claim_id, denial_date, denial_reason_code, denial_amount)
        VALUES (?, ?, 'INVALID_BILLING_AMOUNT', ?)
        """,
        [(claim_id, discharge_date, amount) for claim_id, amount, discharge_date in rows],
    )
    return len(rows)


def deny_baseline(conn, rng, reason_code, rate):
    """Rule 2: a small random slice of not-yet-denied claims, for background realism."""
    already_denied = {r[0] for r in conn.execute("SELECT claim_id FROM denials").fetchall()}
    all_claims = conn.execute(
        "SELECT claim_id, billing_amount, discharge_date FROM claims ORDER BY claim_id"
    ).fetchall()
    eligible = [c for c in all_claims if c[0] not in already_denied]

    n = round(len(all_claims) * rate)
    chosen = rng.sample(eligible, min(n, len(eligible)))
    conn.executemany(
        """
        INSERT INTO denials (claim_id, denial_date, denial_reason_code, denial_amount)
        VALUES (?, ?, ?, ?)
        """,
        [(claim_id, discharge_date, reason_code, amount) for claim_id, amount, discharge_date in chosen],
    )
    return len(chosen)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM denials")  # rerunning this script alone stays idempotent
    rng = random.Random(RANDOM_SEED)

    flipped, target, total = seed_segment_spike(conn, rng)
    print(
        f"Seeded spike in {SPIKE_SEGMENT}: flipped {flipped} claims to negative "
        f"billing (target ~{target}/{total} ≈ {SPIKE_TARGET_RATE:.0%})"
    )

    n_invalid = deny_negative_billing(conn)
    print(f"INVALID_BILLING_AMOUNT: {n_invalid} claims denied")

    n_audit = deny_baseline(conn, rng, "RANDOM_AUDIT", RANDOM_AUDIT_RATE)
    print(f"RANDOM_AUDIT: {n_audit} claims denied")

    n_risk = deny_baseline(conn, rng, "HIGH_RISK_SCORE", HIGH_RISK_SCORE_RATE)
    print(f"HIGH_RISK_SCORE: {n_risk} claims denied")

    conn.commit()
    conn.close()

    total_denied = n_invalid + n_audit + n_risk
    print(f"\nTotal denials: {total_denied} ({total_denied / 55500:.1%} of all claims)")


if __name__ == "__main__":
    main()
