"""
Tests for src/agents/sentinel.py (docs/architecture/lld-sprint2.md §1).

Four layers of proof, cheapest/most isolated first, matching the module's
own three-layer split (pure math -> DB loading -> orchestration):

  1. PURE MATH — two_proportion_z_test() against hand-computed numbers.
     No database, no segment names, no I/O at all. Proves the formula
     itself is correct in total isolation.
  2. SYNTHETIC DATA — run_sentinel() against a from-scratch, in-memory
     sqlite3 database with made-up provider/condition strings that appear
     nowhere else in this codebase or the LLD. This is the load-bearing
     proof that detection EMERGES from the statistics rather than
     recognizing "UnitedHealthcare"/"diabetes"/"Cigna"/"obesity" as special
     strings: if sentinel.py had a hardcoded backdoor for those names, this
     test's entirely different, fabricated segment would never be flagged.
  3. REAL-DATA INTEGRATION — against the actual, already-regenerated,
     committed src/datahub/healthcare.db (Slice 0's output). Confirms both
     real seeded segments are flagged, with z-scores landing close to
     Slice 0's independently-computed src/datahub/verify_sentinel_math.py
     output (z≈35.53, z≈25.69) — same computation, run through the real
     module instead of the throwaway proof script.
  4. REAL NEGATIVE CASE — UnitedHealthcare/cancer, the real segment Slice 0
     found scored most negative (z≈-4.21). Confirms Sentinel does NOT flag
     a genuinely clean segment, using real data rather than only a
     synthetic one.
"""

import sqlite3
from pathlib import Path

import pytest

from agents.sentinel import (
    Segment,
    default_summary,
    load_segment_counts,
    run_sentinel,
    two_proportion_z_test,
)

REAL_DB_PATH = Path(__file__).parent.parent / "src" / "datahub" / "healthcare.db"


# ===========================================================================
# 1. Pure math — no DB, no segment names, no I/O.
# ===========================================================================


class TestTwoProportionZTest:
    """two_proportion_z_test(segment_claims, segment_denials, total_claims,
    total_denials) -> float. Every case here is either hand-computable or
    checkable by an independent script — see each test's comment."""

    def test_hand_computed_example(self):
        """segment: 100 claims, 20 denied (20% rate).
        total:   1000 claims, 100 denied  =>  rest: 900 claims, 80 denied (8.89% rate).

        Hand-worked:
            p_segment = 20/100        = 0.2
            p_rest    = 80/900        = 0.088888...
            p_pool    = 100/1000      = 0.1
            se        = sqrt(0.1 * 0.9 * (1/100 + 1/900)) = sqrt(0.001) = 0.0316227766...
            z         = (0.2 - 0.088888...) / 0.0316227766... = 3.513641844631532...

        Independently reproducible with a four-line script and a calculator —
        no dependency on this test's own code being correct.
        """
        z = two_proportion_z_test(segment_claims=100, segment_denials=20, total_claims=1000, total_denials=100)
        assert z == pytest.approx(3.513641844631532, abs=1e-9)

    def test_zero_when_segment_rate_equals_rest_rate(self):
        """If a segment's denial rate exactly matches everyone else's, there's
        nothing to distinguish it from the pack — z must be exactly 0.0.
        segment: 500/50 (10%). rest (total 1000/100 minus segment): 500/50 (10%), identical.
        """
        z = two_proportion_z_test(segment_claims=500, segment_denials=50, total_claims=1000, total_denials=100)
        assert z == pytest.approx(0.0, abs=1e-9)

    def test_negative_when_segment_rate_is_below_baseline(self):
        """A segment with a LOWER denial rate than the rest should score
        negative, not just "not flagged" — the sign carries real information
        (this is exactly what all 28 non-seeded real segments do, per Slice 0).
        segment: 200 claims, 2 denied (1%). total: 1000/100 => rest 800/98 (12.25%).
        """
        z = two_proportion_z_test(segment_claims=200, segment_denials=2, total_claims=1000, total_denials=100)
        assert z < 0

    def test_degenerate_no_denials_anywhere_returns_zero(self):
        """se == 0 guard: zero denials in the whole population means every
        rate is identically 0% — no variation exists to compute a meaningful
        z from. 0.0 ("not anomalous") is correct, not a crash."""
        z = two_proportion_z_test(segment_claims=50, segment_denials=0, total_claims=500, total_denials=0)
        assert z == 0.0

    def test_degenerate_every_claim_denied_returns_zero(self):
        """Same guard, other edge: 100% denied everywhere."""
        z = two_proportion_z_test(segment_claims=50, segment_denials=50, total_claims=500, total_denials=500)
        assert z == 0.0

    def test_rejects_empty_segment(self):
        with pytest.raises(ValueError):
            two_proportion_z_test(segment_claims=0, segment_denials=0, total_claims=100, total_denials=10)

    def test_rejects_segment_that_is_the_entire_population(self):
        """Leave-one-out needs a non-empty 'rest' — a segment can't be
        100% of the population under this method."""
        with pytest.raises(ValueError):
            two_proportion_z_test(segment_claims=100, segment_denials=10, total_claims=100, total_denials=10)

    def test_rejects_denials_exceeding_claims(self):
        with pytest.raises(ValueError):
            two_proportion_z_test(segment_claims=10, segment_denials=11, total_claims=100, total_denials=20)


# ===========================================================================
# 2. Synthetic data — proves detection generalizes, not hardcoded strings.
# ===========================================================================


def _build_synthetic_db(segments: dict[tuple[str, str], tuple[int, int]]) -> sqlite3.Connection:
    """Build a fresh in-memory sqlite3 DB with the minimal schema
    load_segment_counts() actually needs (claims.claim_id/insurance_provider/
    medical_condition, denials.claim_id) — deliberately NOT the full
    schema_sprint1.sql shape, to keep this test's proof about the statistics,
    not about schema fidelity (that's what the real-data tests below cover).

    `segments`: {(provider, condition): (claim_count, denied_count)}.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE claims (claim_id TEXT PRIMARY KEY, insurance_provider TEXT NOT NULL, medical_condition TEXT NOT NULL)")
    conn.execute("CREATE TABLE denials (denial_id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT NOT NULL)")

    claim_counter = 0
    for (provider, condition), (claim_count, denied_count) in segments.items():
        assert denied_count <= claim_count
        for i in range(claim_count):
            claim_counter += 1
            claim_id = f"SYN-{claim_counter:06d}"
            conn.execute(
                "INSERT INTO claims (claim_id, insurance_provider, medical_condition) VALUES (?, ?, ?)",
                (claim_id, provider, condition),
            )
            if i < denied_count:
                conn.execute("INSERT INTO denials (claim_id) VALUES (?)", (claim_id,))
    conn.commit()
    return conn


# Entirely fabricated provider/condition strings — invented for this test,
# not found anywhere else in this repo or in lld-sprint2.md. If sentinel.py
# ever grew a hardcoded reference to a REAL seeded segment name, this test
# would still have to pass on its own terms (these names, this connection,
# nothing shared with healthcare.db) precisely because it shares nothing
# with the real data at all.
SYNTHETIC_ANOMALY_SEGMENT = ("Zorbex Insurance", "moonflu")
SYNTHETIC_BASELINE_SEGMENTS = {
    ("Blarg Health", "sneezies"),
    ("Quivo Mutual", "wobblejoint"),
    ("Fennec Assurance", "dry_cough"),
    ("Plexico Care", "itchy_scalp"),
    ("Nimbus Health", "foggy_brain"),
}


def test_detects_anomaly_in_synthetic_segment_with_no_relation_to_real_data():
    """The load-bearing proof: a made-up segment with an implanted spike gets
    flagged, and five other made-up segments at ordinary baseline rates do
    not — using a from-scratch in-memory database sentinel.py has never seen
    before, with names that exist only inside this test function. Detection
    can only be working "for real" here, via the GROUP BY + z-test, since
    there is no other mechanism by which sentinel.py could recognize these
    strings as significant.
    """
    segments = {
        SYNTHETIC_ANOMALY_SEGMENT: (300, 90),  # 30% denial rate — the implanted spike
    }
    for seg in SYNTHETIC_BASELINE_SEGMENTS:
        segments[seg] = (300, 15)  # 5% denial rate, identical across all five — the "normal" background

    conn = _build_synthetic_db(segments)
    findings = run_sentinel(conn, z_threshold=3.5)
    conn.close()

    assert len(findings) == 6  # every segment present in the synthetic data, flagged or not

    by_segment = {f.segment: f for f in findings}

    anomaly_finding = by_segment[Segment(*SYNTHETIC_ANOMALY_SEGMENT)]
    assert anomaly_finding.flagged is True
    assert anomaly_finding.z_score > 3.5
    # From an independent hand-computation of this exact scenario (see the
    # module's docstring-adjacent derivation) — z ≈ 13.7, comfortably clear
    # of the 3.5 threshold and nowhere near the baseline segments' scores.
    assert anomaly_finding.z_score == pytest.approx(13.7, abs=0.1)

    for seg in SYNTHETIC_BASELINE_SEGMENTS:
        finding = by_segment[Segment(*seg)]
        assert finding.flagged is False, f"{seg} should NOT be flagged (it's the fabricated baseline, not the spike)"
        assert finding.z_score < 0  # pulled slightly negative by the spike raising their shared "rest" baseline


def test_synthetic_uniform_rates_flags_nothing():
    """A second, cheap synthetic negative case: every segment at (very
    nearly) the same rate, no implanted anomaly at all. Nothing should
    flag. Rates aren't forced bit-for-bit identical (200/10, 200/11, 200/9,
    200/10, 200/11) so the z-test has to actually tolerate ordinary sampling
    noise, not just the trivial all-equal case already covered above.
    """
    segments = {
        ("Aurorabelt Mutual", "glimmerpox"): (200, 10),
        ("Cobblecare", "fizzlecough"): (200, 11),
        ("Driftwood Health", "grumblejaw"): (200, 9),
        ("Ember & Frost Assurance", "static_toe"): (200, 10),
        ("Lark Provident", "whistlelung"): (200, 11),
    }
    conn = _build_synthetic_db(segments)
    findings = run_sentinel(conn, z_threshold=3.5)
    conn.close()

    assert len(findings) == 5
    assert all(not f.flagged for f in findings)


def test_default_z_threshold_read_from_environment(monkeypatch):
    """§1.2: SENTINEL_Z_THRESHOLD is a real, live-tunable config value, read
    via load_dotenv()/os.environ — not a value baked in at import time.
    Setting it very low (0.0) should flag even a middling segment that a
    threshold of 3.5 wouldn't."""
    monkeypatch.setenv("SENTINEL_Z_THRESHOLD", "0.0")
    segments = {
        ("Zorbex Insurance", "moonflu"): (300, 20),  # only mildly elevated, not a real spike
        ("Blarg Health", "sneezies"): (300, 15),
    }
    conn = _build_synthetic_db(segments)
    findings = run_sentinel(conn)  # no explicit z_threshold -> must read the env var
    conn.close()

    by_segment = {f.segment: f for f in findings}
    assert by_segment[Segment("Zorbex Insurance", "moonflu")].threshold == 0.0
    assert by_segment[Segment("Zorbex Insurance", "moonflu")].flagged is True  # z > 0.0 given a higher-than-baseline rate


def test_narrate_fn_override_is_used_when_provided():
    """§1.4's narration seam: run_sentinel() accepts an optional narrate_fn
    that, if given, replaces default_summary() entirely — proving the
    extension point is real and wired through, even though nothing in this
    slice supplies an LLM-backed one yet."""
    segments = {
        ("Zorbex Insurance", "moonflu"): (300, 90),
        ("Blarg Health", "sneezies"): (300, 15),
    }
    conn = _build_synthetic_db(segments)
    findings = run_sentinel(conn, z_threshold=3.5, narrate_fn=lambda finding: "CUSTOM NARRATION")
    conn.close()

    assert all(f.summary == "CUSTOM NARRATION" for f in findings)


def test_default_summary_wording_differs_for_flagged_vs_not():
    """default_summary() itself, exercised directly (not through run_sentinel)
    — a flagged finding's summary should read like an alert; a non-flagged
    one should read like a routine, reassuring status line. Built from
    fabricated field values directly, no DB involved at all.
    """
    flagged = _make_finding(flagged=True, z_score=13.7, segment_denial_rate=0.30, baseline_denial_rate=0.05)
    not_flagged = _make_finding(flagged=False, z_score=-2.7, segment_denial_rate=0.05, baseline_denial_rate=0.062)

    assert "unlikely to be random variation" in default_summary(flagged)
    assert "within normal variation, not flagged" in default_summary(not_flagged)


def _make_finding(*, flagged, z_score, segment_denial_rate, baseline_denial_rate):
    from agents.sentinel import METHOD, SentinelFinding

    return SentinelFinding(
        segment=Segment("Zorbex Insurance", "moonflu"),
        segment_claim_count=300,
        segment_denial_count=round(300 * segment_denial_rate),
        segment_denial_rate=segment_denial_rate,
        baseline_denial_rate=baseline_denial_rate,
        z_score=z_score,
        threshold=3.5,
        method=METHOD,
        flagged=flagged,
        summary="",
    )


# ===========================================================================
# 3. Real-data integration — against the committed, already-regenerated
#    src/datahub/healthcare.db (Slice 0's output).
# ===========================================================================


@pytest.fixture(scope="module")
def real_conn():
    # Mirrors tests/test_sprint1.py's own fixture convention exactly: a clear
    # assertion failure ("run the pipeline first") rather than sqlite3
    # silently creating an empty file and failing later with a confusing
    # "no such table" error.
    assert REAL_DB_PATH.exists(), (
        f"healthcare.db not found at {REAL_DB_PATH} — Slice 0's seed sequence "
        "(seed_upstream_scenario.py, schema_sprint1.sql, generate_denials.py, "
        "score_claims.py) must have already run and been committed."
    )
    connection = sqlite3.connect(REAL_DB_PATH)
    yield connection
    connection.close()


def test_real_run_scans_all_30_segments_and_flags_only_the_two_seeded(real_conn):
    """The whole-picture check: run Sentinel across the real data exactly as
    a future Orchestrator (Slice 4) would, and confirm the flagged set is
    EXACTLY the two segments Slice 0 seeded — no more, no fewer."""
    findings = run_sentinel(real_conn, z_threshold=3.5)
    assert len(findings) == 30

    flagged_segments = {f.segment for f in findings if f.flagged}
    assert flagged_segments == {
        Segment("UnitedHealthcare", "diabetes"),
        Segment("Cigna", "obesity"),
    }


def test_real_seeded_segments_z_scores_match_slice0_verification(real_conn):
    """z-scores should land close to src/datahub/verify_sentinel_math.py's
    independently-computed output (z≈35.53, z≈25.69) — same formula, same
    committed data, run through the real module instead of the throwaway
    proof script. A generous-but-real tolerance (±0.5): this should be
    close to exact (both read the same committed healthcare.db), not just
    "in the right ballpark".
    """
    findings = run_sentinel(real_conn, z_threshold=3.5)
    by_segment = {f.segment: f for f in findings}

    uhc_diabetes = by_segment[Segment("UnitedHealthcare", "diabetes")]
    assert uhc_diabetes.flagged is True
    assert uhc_diabetes.z_score == pytest.approx(35.53, abs=0.5)

    cigna_obesity = by_segment[Segment("Cigna", "obesity")]
    assert cigna_obesity.flagged is True
    assert cigna_obesity.z_score == pytest.approx(25.69, abs=0.5)


# ===========================================================================
# 4. Real negative case — a genuinely clean real segment.
# ===========================================================================


def test_real_clean_segment_is_not_flagged(real_conn):
    """UnitedHealthcare/cancer is the real segment Slice 0's
    verify_sentinel_math.py found scored MOST negative (z≈-4.21) — the
    segment furthest from anomalous in the entire real dataset. Confirms
    Sentinel correctly does NOT flag it, using real data (not just the
    synthetic negative cases above)."""
    findings = run_sentinel(real_conn, z_threshold=3.5)
    by_segment = {f.segment: f for f in findings}

    finding = by_segment[Segment("UnitedHealthcare", "cancer")]
    assert finding.flagged is False
    assert finding.z_score == pytest.approx(-4.21, abs=0.5)


def test_load_segment_counts_against_real_db_returns_30_segments(real_conn):
    """A focused unit test of the DB-loading layer alone (not run_sentinel's
    full orchestration) — confirms load_segment_counts() on its own returns
    sane (claim_count, denied_count) pairs for all 30 real segments, each
    claim_count in the expected 1,740-1,907 range documented in
    lld-sprint2.md §1.1."""
    counts = load_segment_counts(real_conn)
    assert len(counts) == 30
    for segment, (claim_count, denied_count) in counts.items():
        assert 1740 <= claim_count <= 1907, f"{segment} has an unexpected claim_count: {claim_count}"
        assert 0 <= denied_count <= claim_count
