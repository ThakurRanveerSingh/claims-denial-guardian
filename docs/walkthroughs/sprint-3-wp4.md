# Sprint 3, WP4 — Model drift / feature-health check (and Sprint 3's close)

Built against `docs/architecture/lld-sprint3-wp4.md` and
`docs/decisions/0011-drift-check-design.md`, implementing US-6 — the last
work package of Sprint 3. Two parts, split by an explicit stop the repo
owner asked for: the LLD (design only, reviewed before any code), then
implementation once "go" was given. This closes Sprint 3 entirely — all
four work packages (Scribe, Remediator, Reporter, Drift) are now built,
tested, and proven live.

## What was built

| Stage | Files | Purpose |
|---|---|---|
| LLD | `docs/architecture/lld-sprint3-wp4.md` | Design + the honesty verdict the repo owner asked to read first: no genuine temporal baseline exists for this dataset |
| Investigation detour | `docs/decisions/0010-regeneration-non-determinism.md`, `src/datahub/README.md` | A real bug found while verifying the LLD's baseline question: the README's "regenerate from scratch" instructions silently produce a different db than every committed artifact, contradicting its own reproducibility claim |
| Implementation | `src/agents/drift.py` | `DriftFinding`/`FeatureHealthCheck`, feature list read from DataHub, the checks themselves, writeback to `denial_model_scores`/`denial_risk_model` |
| Implementation | `src/agents/orchestrator.py` | `Incident.drift`, `attach_drift_finding()` |
| Implementation | `src/agents/cli.py` | `guardian check-drift [--incident <id>]` |
| Implementation | `src/agents/reporter.py` + templates | Model Health Check section, same plain-English/appendix split decision 0009 established |
| Proof | Both canonical incidents | Real `guardian check-drift --incident <id>` runs, live idempotency proof, regenerated reports |
| Close-out | `docs/decisions/0011-drift-check-design.md`, this file | What was actually built, once real behavior existed to document |

Test suite: **374 tests**, 1 more `@pytest.mark.live` (excluded by default,
run explicitly this session against the real DataHub).

## Part A: the LLD, and a detour that mattered

The repo owner's spec was explicit about what NOT to build: "one drift
signal, not a monitoring suite... do not force a drift story if the data
doesn't support one." Point 1 asked for something specific and unusual
for this project — empirical verification of a claim before trusting it:
"does regenerating/reseeding change these distributions at all? verify
empirically before assuming yes."

Investigating that turned up something the LLD itself didn't anticipate:
`src/datahub/README.md` contained a "Generate from Scratch" section that,
if followed, would silently produce a DIFFERENT `healthcare.db` than the
one every artifact in this repo (every incident, PR, audit report) was
computed against — and the README's own claim that "all issues use
`random.seed(42)` for reproducibility" turned out to be false: the actual
row-selection mechanism is SQLite's `ORDER BY RANDOM()`, a separate PRNG
Python's seeding has zero effect on. Confirmed directly, not assumed:
identical `random.seed(42)` calls before two runs of the same query still
returned different rows. Fixed as decision 0010, before continuing — the
repo owner's own framing for this ("a sharp judge running our own
instructions could stumble into this") made it clear this couldn't wait
for a later session.

The LLD's own honesty verdict — after this detour, and after correcting a
mistake made along the way (an early test claimed the
`generate_denials.py`/`score_claims.py` layer was non-deterministic; it
wasn't — the test had skipped a documented reset step Sprint 2's own
walkthrough already warns against skipping, and redone correctly it
proved byte-identical) — was unambiguous: **no genuine "baseline vs.
current" temporal comparison is possible with this dataset.** Proposed
instead: single-snapshot internal-consistency checks against documented
expected ranges, exactly the "smallest honest version" the spec itself
suggested as the fallback. Approved with "Go — the honesty verdict
stands as originally reported... the regeneration scare... has been
resolved in our favor."

## Part B/C: implementation, and a live-caught schema constraint

Built `src/agents/drift.py` per the LLD: a naive mean/std check on
`billing_zscore` was proven — algebraically AND against the live data,
not just assumed — to be tautological (per-segment z-scoring guarantees
pooled mean=0/std=1 regardless of the data's real health), so it was
rejected in favor of a cap-exceedance check against the model's own
documented boundary and a PSI-based shape comparison against the
theoretical standard normal. `segment_denial_rate`'s range check was kept
but explicitly reframed as a corruption guard, not a drift signal — it's
mathematically guaranteed to pass under correct code.

The first live test of the writeback path (not the mocked test suite —
those necessarily encode assumptions about what the real system accepts)
surfaced a real bug: DataHub rejected the assertion writeback with a 422,
`Required: [dataset]` — a `CustomAssertionInfo`'s `entity` field must be a
dataset-typed URN, and `denial_risk_model` is an MLModel, not a dataset.
Fixed by resolving `denial_model_scores` (the real dataset holding the
actual feature values) via live MCP search and building real schemaField
URNs from its columns — the same discipline Scribe's own billing-amount
assertion already uses, just applied to a different table. The tag and
documentation note stay on `denial_risk_model` itself; no such constraint
applies there. Full reasoning: decision 0011 §5.

## Proof: live, twice, against both real incidents

`guardian check-drift --incident <id>` was run for real against both
canonical incidents (UnitedHealthcare/diabetes, Cigna/obesity) — not
simulated. Both show all three checks passing (segment_denial_rate range
clean; billing_zscore 0.40% beyond the documented cap, PSI 0.038 against
the 0.10 threshold). Idempotency was verified directly, not inferred: the
identical `DriftFinding` object was passed through `run_drift_writeback()`
twice, and the second call correctly reported the tag, documentation
note, and both assertions as already present — zero duplicate writes,
confirmed against the real DataHub instance, the same standard this
project's every prior writeback stage (Scribe, Remediator's idempotent
PR-reuse) has already been held to.

One stale test assumption surfaced by this: `TestModelHealthSection`'s
"no drift finding" test had asserted the real canonical incidents would
never have one attached — true when written, false the moment the live
CLI run above actually attached one. Fixed by forcing `drift=None`
explicitly for that specific test (the absence-render-path is what's
under test, independent of which incidents happen to have a check
attached at any given moment) — the same "re-validate against the
system's real current state, don't leave a stale assumption standing"
discipline this project applied once already in Sprint 3 WP3.

## Sprint 3, closed

All four work packages — Scribe (US-4), Remediator (US-3), Reporter
(US-5), Drift (US-6) — are built, tested (374 tests, 6 marked `@pytest.
mark.live`), and each has been proven against the real, live DataHub
instance and the real committed `healthcare.db`, not just a mocked test
suite. Every UAT/live-testing finding across the sprint left behind a
regression test, not just a fix — the standing instruction from WP3
applied consistently through to the sprint's last work package.
