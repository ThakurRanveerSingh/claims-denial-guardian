#!/usr/bin/env python3
"""
Sentinel — anomaly detection over per-segment claim denial rates.
Design: docs/architecture/lld-sprint2.md §1.

What this module does, in one sentence: for every (insurance_provider,
medical_condition) segment present in `claims`/`denials`, compare that
segment's denial rate against a leave-one-out baseline (every OTHER
segment's pooled rate) using a two-proportion z-test, and flag segments
whose z-score clears a configured threshold.

Deliberately three separated layers (§1.2's formula promoted from Slice 0's
throwaway src/datahub/verify_sentinel_math.py into this real module, but
restructured rather than copy-pasted):

  1. `two_proportion_z_test()` — pure math. Takes four integers, returns a
     float. No SQL, no segment names, no I/O of any kind. This is what makes
     it possible to unit-test the formula itself in total isolation (hand-
     computable numbers, no database needed at all).
  2. `load_segment_counts()` — the only function that touches the database.
     One GROUP BY query; returns per-segment (claim_count, denied_count)
     pairs. Knows nothing about statistics.
  3. `run_sentinel()` — orchestration. Loops over whatever segments
     `load_segment_counts()` returns, calls the pure math function for each,
     and assembles `SentinelFinding` records. Has no hardcoded segment names
     anywhere — it finds out what segments exist from the data itself, at
     call time, via `GROUP BY`. This is the direct mechanism by which
     detection "emerges from the statistics" rather than recognizing
     particular strings: swap in any (provider, condition) values in the
     underlying tables and this code runs identically. See
     tests/test_sentinel.py's synthetic-data test for the proof.

No LLM calls anywhere in this module (§1.4: the detection decision itself is
a deterministic, closed-form comparison — routing it through a model would
make it slower, non-free, and non-deterministic for a problem that already
has one provably correct answer). The one narration seam this module leaves
open (`narrate_fn` on `run_sentinel`) is inert in this slice — see its
docstring below for why.
"""

import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

from dotenv import load_dotenv

# Same load_dotenv() convention as src/datahub/add_lineage.py, add_metadata.py,
# register_ml_model.py — one pattern for reading .env across the whole repo,
# not a second one invented here. .env is gitignored; SENTINEL_Z_THRESHOLD is
# never hardcoded as a literal default deep in this module's logic, only as
# the documented fallback below if .env doesn't set it.
load_dotenv()

METHOD = "two_proportion_z_test"  # SentinelFinding.method — named explicitly so a
                                   # later reader can tell which detector produced a finding


class Segment(NamedTuple):
    """(insurance_provider, medical_condition) — §1.1's segment definition.

    A NamedTuple rather than a plain 2-tuple or a dict: `segment.insurance_provider`
    reads far clearer at every call site than `segment[0]`, and unlike a dict
    it's hashable — load_segment_counts() uses Segment directly as a dict key,
    which a dict-typed segment couldn't be.
    """

    insurance_provider: str
    medical_condition: str


@dataclass
class SentinelFinding:
    """lld-sprint2.md §1.3's output contract, implemented for real (the LLD
    left this illustrative/not-code; this is the first slice that needs an
    actual object other code can construct, pass around, and assert on)."""

    segment: Segment
    segment_claim_count: int
    segment_denial_count: int
    segment_denial_rate: float
    baseline_denial_rate: float  # leave-one-out, per §1.2
    z_score: float
    threshold: float             # the SENTINEL_Z_THRESHOLD actually used for this run —
                                  # recorded, not just applied, so a later reader can
                                  # audit *why* a segment was (or wasn't) flagged
    method: str
    flagged: bool
    summary: str


# ---------------------------------------------------------------------------
# 1. Pure math — zero knowledge of segment names, zero DB access.
# ---------------------------------------------------------------------------


def two_proportion_z_test(
    segment_claims: int, segment_denials: int, total_claims: int, total_denials: int
) -> float:
    """§1.2's exact formula, leave-one-out baseline.

    Leave-one-out means: the segment under test is excluded from BOTH the
    baseline rate (p_rest) and the standard-error calculation (se) — not
    folded into a single "segment vs. whole population" comparison. §1.2
    explains why: if the segment's own inflated rate were included in the
    population it's being measured against, a real spike would inflate its
    own baseline and dilute the very signal being measured.

    Takes only counts, returns only a number — no segment names, no SQL, no
    knowledge that "segments" are even a concept. This is what makes it
    possible to hand-verify with independently-computable numbers (see
    tests/test_sentinel.py's pure unit test) rather than trusting it only
    because it happens to produce plausible-looking output against real data.
    """
    if segment_claims <= 0:
        raise ValueError("segment_claims must be > 0")
    if total_claims <= segment_claims:
        raise ValueError(
            "total_claims must exceed segment_claims — leave-one-out needs a non-empty 'rest' population"
        )
    if not (0 <= segment_denials <= segment_claims):
        raise ValueError("segment_denials must be between 0 and segment_claims")
    if not (0 <= total_denials <= total_claims):
        raise ValueError("total_denials must be between 0 and total_claims")
    if segment_denials > total_denials:
        raise ValueError("segment_denials cannot exceed total_denials")

    rest_claims = total_claims - segment_claims
    rest_denials = total_denials - segment_denials

    p_segment = segment_denials / segment_claims
    p_rest = rest_denials / rest_claims
    p_pool = (segment_denials + rest_denials) / total_claims

    se = (p_pool * (1 - p_pool) * (1 / segment_claims + 1 / rest_claims)) ** 0.5

    # Degenerate case: se == 0 only happens when p_pool is exactly 0 or 1 —
    # i.e. EVERY claim in the whole dataset is denied, or NONE are. In either
    # case p_segment and p_rest are then also both exactly 0 or both exactly
    # 1 (there's no denial/non-denial variation anywhere to distinguish any
    # segment from any other), so "not anomalous" (z = 0.0) is the correct,
    # honest answer — not a crash. Guarded explicitly rather than left to
    # raise ZeroDivisionError, since this is a real (if unlikely) input shape,
    # not a caller bug.
    if se == 0:
        return 0.0

    return (p_segment - p_rest) / se


# ---------------------------------------------------------------------------
# 2. DB loading — the only function that touches the database. Knows nothing
#    about statistics.
# ---------------------------------------------------------------------------


def load_segment_counts(conn: sqlite3.Connection) -> dict[Segment, tuple[int, int]]:
    """One GROUP BY query: {Segment: (claim_count, denied_count)} for every
    segment actually present in `claims`/`denials` — not a fixed list. This
    is the concrete mechanism behind "no hardcoded segment names": whatever
    distinct (insurance_provider, medical_condition) pairs exist in the data
    at call time are exactly the segments scored, nothing more and nothing
    less.

    On the "never hardcode DataHub schemas" rule: this function does write
    fixed column/table names (`claims.insurance_provider`,
    `claims.medical_condition`, `claims.claim_id`, `denials.claim_id`)
    directly into SQL, rather than confirming them live via the DataHub MCP
    server first. That's a deliberate, scoped choice, not an oversight of the
    rule: lld-sprint2.md §2.2 assigns live schema-confirmation specifically to
    Investigator, which walks lineage across datasets it discovers as it
    goes. Sentinel is different — it only ever reads the two tables
    schema_sprint1.sql itself defines in this repo, the exact same tables
    generate_denials.py/score_claims.py/verify_sentinel_math.py (Sprint 1 and
    Slice 0, both already reviewed) already query directly by name. There's
    no live/unknown schema being guessed at here, just the same fixed,
    version-controlled DDL every other script in this codebase already
    depends on the same way.

    COUNT(DISTINCT ...) rather than Slice 0's verify_sentinel_math.py's
    SUM(CASE WHEN ... ) pattern: a small, deliberate hardening now that this
    is the real module rather than a throwaway proof script. SUM(CASE...)
    silently over-counts if a claim ever had more than one denials row (the
    LEFT JOIN would multiply that claim's row); COUNT(DISTINCT claim_id) is
    correct regardless. schema_sprint1.sql's data never produces duplicate
    denials per claim (tests/test_sprint1.py::test_no_duplicate_denials_per_claim
    guards against a regression there), so this doesn't change any real
    result — it just removes an assumption this function doesn't need to make.
    """
    rows = conn.execute(
        """
        SELECT c.insurance_provider, c.medical_condition,
               COUNT(DISTINCT c.claim_id) AS total,
               COUNT(DISTINCT d.claim_id) AS denied
        FROM claims c
        LEFT JOIN denials d ON d.claim_id = c.claim_id
        GROUP BY c.insurance_provider, c.medical_condition
        """
    ).fetchall()
    return {Segment(provider, condition): (total, denied) for provider, condition, total, denied in rows}


# ---------------------------------------------------------------------------
# Narration — deterministic by default (§1.4).
# ---------------------------------------------------------------------------


def default_summary(finding: SentinelFinding) -> str:
    """The deterministic string template §1.4 recommends as the DEFAULT (not
    just a fallback): a bare `claude -p` call with zero real work still cost
    real money from fixed context-loading overhead alone (measured in §0).
    Spending that — or even a cheaper direct API call — on a routine one-line
    summary, on every pipeline run, by default, is a bad trade this template
    avoids entirely. Covers the actual acceptance criterion (segment +
    magnitude, in plain language) with zero LLM dependency, zero added
    latency, zero added cost.
    """
    provider, condition = finding.segment
    # baseline_denial_rate is 0.0 only in the degenerate all-clean-baseline
    # case (see two_proportion_z_test's se==0 guard) — guard the ratio the
    # same way rather than dividing by zero for a real, if rare, input shape.
    ratio = finding.segment_denial_rate / finding.baseline_denial_rate if finding.baseline_denial_rate > 0 else float("inf")

    if finding.flagged:
        return (
            f"Denial rate for {provider}/{condition} is {finding.segment_denial_rate:.1%}, "
            f"{ratio:.1f}x baseline (z={finding.z_score:.2f}) — statistically very unlikely to be random variation."
        )
    return (
        f"Denial rate for {provider}/{condition} is {finding.segment_denial_rate:.1%} "
        f"({ratio:.1f}x baseline, z={finding.z_score:.2f}) — within normal variation, not flagged."
    )


# ---------------------------------------------------------------------------
# 3. Orchestration.
# ---------------------------------------------------------------------------


def _default_z_threshold() -> float:
    """Read SENTINEL_Z_THRESHOLD from the environment at CALL time, not once
    at import time — lets tests override it per-test via monkeypatch.setenv()
    without needing to reload this module, and matches §1.2's framing of the
    threshold as a real, live-tunable config value rather than a constant
    baked in when the process started."""
    return float(os.environ.get("SENTINEL_Z_THRESHOLD", "3.5"))


def run_sentinel(
    conn: sqlite3.Connection,
    z_threshold: Optional[float] = None,
    narrate_fn: Optional[Callable[[SentinelFinding], str]] = None,
) -> list[SentinelFinding]:
    """Run the two-proportion z-test across EVERY segment present in
    `claims`/`denials` — flagged and not-flagged alike. Matches §4.3's CLI
    design ("run Sentinel across all 30 segments; if any flagged, run
    Investigator on each") and keeps this function directly usable by a
    later Orchestrator (Slice 4, not built yet) without this interface
    needing to change shape when that's added.

    z_threshold: overrides SENTINEL_Z_THRESHOLD for this call only. Same
    "explicit per-run override, .env default otherwise" shape §4.3 uses for
    --max-budget-usd — None (the default) means "use whatever's configured",
    not "use a hardcoded fallback that ignores config".

    narrate_fn: an optional hook for an LLM-generated summary in place of
    default_summary()'s deterministic template — the concrete extension
    point §1.4 designs for a future SENTINEL_NARRATION_ENABLED code path.
    Intentionally NOT wired to anything in this slice: src/agents/llm_backend.py
    doesn't exist yet (Slice 2). This parameter exists so that when it does,
    the LLM-backed narrator can be plugged in here without changing this
    function's signature — not a stub, not partially-built, just an unused
    seam on purpose.
    """
    if z_threshold is None:
        z_threshold = _default_z_threshold()

    segment_counts = load_segment_counts(conn)
    total_claims = sum(claims for claims, _ in segment_counts.values())
    total_denials = sum(denials for _, denials in segment_counts.values())

    findings = []
    for segment, (segment_claims, segment_denials) in segment_counts.items():
        z = two_proportion_z_test(segment_claims, segment_denials, total_claims, total_denials)

        rest_claims = total_claims - segment_claims
        rest_denials = total_denials - segment_denials

        finding = SentinelFinding(
            segment=segment,
            segment_claim_count=segment_claims,
            segment_denial_count=segment_denials,
            segment_denial_rate=segment_denials / segment_claims,
            baseline_denial_rate=rest_denials / rest_claims,
            z_score=z,
            threshold=z_threshold,
            method=METHOD,
            flagged=z > z_threshold,
            summary="",  # filled in immediately below, once every other field is set
        )
        finding.summary = narrate_fn(finding) if narrate_fn else default_summary(finding)
        findings.append(finding)

    return findings


def main():
    """Standalone smoke-run against the real, committed healthcare.db — same
    "runnable script with a clear summary" convention as src/datahub/
    generate_denials.py, score_claims.py, verify_sentinel_math.py. Not the
    CLI entrypoint (`guardian run` is §4.3, Slice 4, a separate script) —
    just a quick way to see Sentinel's real output without writing a test.
    """
    db_path = os.path.join(os.path.dirname(__file__), "..", "datahub", "healthcare.db")
    conn = sqlite3.connect(db_path)
    findings = run_sentinel(conn)
    conn.close()

    findings.sort(key=lambda f: f.z_score, reverse=True)
    flagged = [f for f in findings if f.flagged]

    print(f"Sentinel scanned {len(findings)} segments, {len(flagged)} flagged (threshold={findings[0].threshold}):\n")
    for f in findings:
        marker = "FLAGGED" if f.flagged else "      "
        print(f"  [{marker}] {f.segment.insurance_provider}/{f.segment.medical_condition}  z={f.z_score:>7.2f}")
        if f.flagged:
            print(f"            {f.summary}")


if __name__ == "__main__":
    main()
