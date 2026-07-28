# Low-Level Design — Sprint 3, WP4: Model Drift Check (US-6)

Status: **Design only — not yet implemented.** Written per the repo owner's
explicit instruction to stop after this document so the honesty verdict in
§5 can be reviewed before any code is written.

A new, standalone file rather than an addendum to `lld-sprint2.md`: that
file is scoped to Sentinel/Investigator (Sprint 2). Scribe, Remediator, and
Reporter each got their own decision doc instead of an lld-sprint2.md
addendum (`0007`, `0008`, `0009`) — this follows the same precedent, as an
LLD rather than a decision doc because it's written *before*
implementation, not after (WP1–3's decision docs were all written at
close-out, once real behavior existed to document).

## 0. Scope, restated

Intentionally minimal, per the original Sprint 3 brief: **one drift
signal, not a monitoring suite.** This document covers what "drift" means
for this system concretely (§1), the test itself (§2), the `DriftFinding`
structure and writeback (§3), what's explicitly out of scope (§4), and the
honesty verdict on whether a real baseline exists (§5) — read that section
first if short on time; it's the reason this stayed a design doc instead
of going straight to code.

## 1. What "drift" means here — investigated, not assumed

The two named features, confirmed live against DataHub rather than
hardcoded from memory of the registration script:

```
$ mcp__datahub__search "/q denial_risk_features"
urn:li:mlFeatureTable:(urn:li:dataPlatform:python,denial_risk_features)
  mlFeatures:
    urn:li:mlFeature:(denial_risk_features,segment_denial_rate)
    urn:li:mlFeature:(denial_risk_features,billing_zscore)
```

Exactly the two features named in the WP4 spec, and exactly the two
`FEATURE_DEFINITIONS` entries in `src/datahub/register_ml_model.py` —
confirming that script's registration matches what's actually live. §3.1
below designs how the implementation reads this list from DataHub at
check-time rather than hardcoding it, per point 1's instruction.

The `denial_risk_model` MLModel entity's own registered description
already states its honesty plainly, which is worth quoting because it
constrains what a "drift" check can honestly claim about this model:

> "Toy denial-risk scorer (model_version 'toy-denial-risk-v1'). NOT a
> trained model — a deterministic, explicitly untuned weighted-sum
> heuristic: risk_score = clamp01(0.6 × segment_denial_rate + 0.4 ×
> min(|billing_zscore| / 4, 1.0))."

There is no training step, no historical train/test split, and (per §5)
no independent second time-point sample. "Drift" in the conventional ML
sense — has the *real-world* distribution feeding this model shifted
since it was fit — doesn't apply to a model that was never fit to
anything. What *does* apply, and is checked here: does the CURRENT
feature data still satisfy the mathematical and code-documented
properties it's supposed to satisfy. That's a narrower, honester claim,
and it's the one this design makes.

## 2. The test(s) — and why the obvious one is rejected

### 2.1 Rejected first: naive mean/std shift on `billing_zscore`

The spec suggested "a mean/std shift check or PSI, whichever fits the
data honestly." For `billing_zscore` specifically, mean/std is **not just
unhelpful, it's tautological** — verified both algebraically and
empirically before writing this section, not assumed:

`score_claims.py`'s `compute_segment_stats()` computes `billing_zscore`
as `(billing_amount - segment_mean) / segment_pstdev`, using
`statistics.pstdev` (population, divide-by-n) **within each segment**.
Z-scoring a set against its own population mean/stdev makes that set's
mean exactly 0 and its (population) variance exactly 1 — an algebraic
identity of the transform, true regardless of what the underlying billing
amounts look like. Summing across segments preserves both properties
(each segment's deviations already sum to zero; each segment's squared-
deviation sum already equals its own claim count under pstdev), so the
*pooled* mean/std across all 55,500 claims is also guaranteed ≈0/1.

Confirmed against the live `healthcare.db`, not just derived on paper:

```
pooled mean(billing_zscore)   = -1.1e-17   (floating-point zero)
pooled pstdev(billing_zscore) =  1.0000000000000
```

A "mean≈0, std≈1" check on this feature would pass with the same two
numbers whether the pipeline is completely healthy or badly broken, as
long as `score_claims.py` ran at all — it's checking that the code
executed, not that the data is sane. This is exactly the "whichever
sounds more impressive" trap the spec warned against avoiding, so it's
rejected in favor of two checks that actually carry information:

### 2.2 Chosen: cap-exceedance + PSI-vs-theoretical-shape, both single-snapshot

**`billing_zscore` check A — cap exceedance.** `score_claims.py` already
declares `BILLING_ZSCORE_CAP = 4.0` as the documented boundary of the
"meaningful range" (`normalized_zscore = min(|z|/CAP, 1.0)` — beyond the
cap, every claim contributes identically to risk regardless of how far
out it actually is). Counting claims whose *raw* `billing_zscore` exceeds
that already-documented cap is a real, non-tautological, zero-new-
threshold check — it reuses a boundary the model's own code already
committed to, rather than inventing one.

Measured against the live data: **224 of 55,500 claims (0.40%)** exceed
`|z| > 4`. Flag threshold: none hard-coded beyond "this number is
reported plainly" for v1 — see §4 on why no alerting is being added.

**`billing_zscore` check B — shape vs. theoretical N(0,1), via PSI.**
Z-scoring fixes the first two moments (mean, variance) by construction —
it does *not* guarantee the distribution's *shape* resembles a normal
curve. That's a genuinely testable, non-circular property, and it's a
legitimate single-snapshot check: no second time point is needed to ask
"does this deterministic transform's output still look like what it's
supposed to approximate," only a comparison against the theoretical
standard normal PSI is measuring shape *against*, not against past data.

Computed against the live data (10 deciles of the standard normal,
observed-vs-theoretical-10%-each):

```
PSI(billing_zscore vs. N(0,1)) = 0.038
skewness = -0.64, excess kurtosis = +1.01
```

Using the widely-published PSI convention (not invented for this
project) — `<0.1` no significant shift, `0.1–0.25` moderate, `>0.25`
significant — 0.038 reads as healthy, with a mild left skew and fat
tails consistent with real per-segment billing-amount data on a modest
sample. `documented_expected` for this check is literally "PSI < 0.10",
citing the standard convention, not a project-invented number.

**`segment_denial_rate` check — range invariant.** `denial_rate =
denied_count / len(rows)`, where `denied_count` is by construction a
count of a subset of `rows` — so `segment_denial_rate ∈ [0.0, 1.0]` is
*also* a mathematical identity of the code, not a distributional fact.
Framed explicitly and honestly as what it is: **not a drift signal, a
corruption/regression guard.** It will always pass while the pipeline's
arithmetic is intact, and that's fine — DataHub assertions elsewhere in
this project (Scribe's billing-amount assertion) already exist for
exactly this purpose: cheap, always-on invariants that are boring when
things work and load-bearing when something breaks (a bad join, a bug
in `compute_segment_stats`, manual data tampering). It is reported as a
health check, never described as detecting distributional change.

Measured against the live data: 10 segments, range **[0.023, 0.208]** —
inside [0,1] as guaranteed, reported for completeness rather than as a
finding.

### 2.3 Zero LLM

All three checks are closed-form arithmetic (counts, a PSI formula, a
range test) against numbers already sitting in `denial_model_scores`. No
LLM call anywhere in this path — consistent with every other Guardian
stage's "LLM proposes (Investigator only), code verifies" boundary, and
here there's nothing for an LLM to propose; it's pure computation.

## 3. `DriftFinding` and writeback

### 3.1 Reading the feature list from DataHub, not hardcoding it

At check time, the implementation queries the live
`denial_risk_features` MLFeatureTable entity (via the MCP server, per
decision 0003 — all DataHub reads go through MCP, never a hardcoded
schema) and extracts the `mlFeatures` URN list. A small dispatch table
maps each feature's leaf name to its check function:

```python
FEATURE_CHECKS: dict[str, Callable[[sqlite3.Connection], FeatureHealthCheck]] = {
    "segment_denial_rate": _check_segment_denial_rate_range,
    "billing_zscore": _check_billing_zscore_health,
}
```

The set of features to check is DataHub's answer, not a Python literal —
consistent with point 1's instruction. The check *implementations*
necessarily stay in code (same as Reporter's `severity_for()` thresholds
already do) — DataHub can tell us a feature named `segment_denial_rate`
exists, but not what a sane range for it is. If DataHub's feature list
ever contains a name with no entry in `FEATURE_CHECKS`, the finding says
so explicitly (`"no check implemented for feature X"`) rather than
silently skipping it — the same quarantine-not-hide discipline decision
0008 established for Remediator.

### 3.2 The structure

```python
@dataclass
class FeatureHealthCheck:
    feature_name: str           # "segment_denial_rate" | "billing_zscore"
    check_type: str             # "range_invariant" | "cap_exceedance" | "shape_vs_theoretical"
    documented_expected: str    # e.g. "[0.0, 1.0]", "|z| <= 4.0 (BILLING_ZSCORE_CAP)", "PSI < 0.10 vs N(0,1)"
    metric_value: float         # the computed number (exceedance %, PSI, observed min/max)
    status: str                 # "pass" | "flagged"
    plain_summary: str          # one sentence, no jargon -- for the audit report narrative

@dataclass
class DriftFinding:
    check_id: str                # f"drift-{utc_timestamp}", keys writeback idempotency
    model_version: str           # "toy-denial-risk-v1", from score_claims.MODEL_VERSION
    checked_at: str              # ISO 8601 UTC
    feature_checks: list[FeatureHealthCheck]
    overall_status: str          # "pass" | "N check(s) flagged"
```

Deliberately flat and small — no history, no trend, no severity
gradient beyond pass/flagged. Matches the spec's "not a monitoring
suite" instruction directly in the data model, not just the CLI surface.

### 3.3 Writeback to `denial_risk_model` — extending Scribe's exact pattern

Same tag / doc-note / assertion conventions `scribe.py` already
established (`_run_scribe_async`, reviewed directly before writing this
section), same idempotency discipline, applied to the MLModel entity
instead of lineage-implicated datasets:

- **Tag** — `urn:li:tag:denial_guardian_drift_checked` applied to
  `urn:li:mlModel:(urn:li:dataPlatform:python,denial_risk_model,PROD)`.
  Idempotent via the same "is it already in `current_tags`" membership
  check `_apply_incident_tag` uses — applied once, not reapplied per run.
- **Doc note** — one `institutionalMemory` entry per check, plain-English
  verdict + `checked_at`, keyed by `check_id` the same way Scribe's notes
  are keyed by `incident_id`: re-running the identical `check_id` is a
  no-op (`doc_note_already_present`), a genuinely new invocation appends
  a new note. This gives a real, visible-in-the-DataHub-UI audit trail of
  checks over time as a *side effect* of repeated on-demand use — not a
  scheduler, nothing runs unless a human or CI step invokes the CLI.
- **Assertion** — one per feature check (`denial_guardian_segment_
  denial_rate_range`, `denial_guardian_billing_zscore_health`), defined
  idempotently once via the same `_assertion_already_exists` MCP check
  Scribe uses, with a fresh assertion run event emitted on every
  invocation (timeseries, deduped server-side on `(assertionUrn,
  timestampMillis, runId)` — the same mechanic already in production use
  for Scribe's billing-amount assertion). This is a natural fit, not a
  stretch: a DataHub assertion *is* "did this data meet an expectation at
  time T," which is precisely what a single-snapshot health check is —
  reusing the concept rather than inventing a parallel one.

### 3.4 Audit report integration

A new optional section, rendered only when `Incident.drift` is set
(mirroring how the Technical Appendix and lineage diagram are only
rendered when the relevant finding exists). Follows decision 0009's
already-established discipline exactly: plain-English `plain_summary`
strings in the main narrative, raw `metric_value`/PSI-bin internals
pushed into the Technical Appendix alongside the existing SQL/tool
trace — the same leak decision 0009 §4 fixed once already shouldn't be
reintroduced by a new section.

### 3.5 `Incident` and CLI wiring

`Incident` gains `drift: Optional[DriftFinding] = None`, following the
exact `scribe`/`remediator` optional-field pattern already in
`orchestrator.py`. `pipeline_stages_run` gains `"drift"` when a check
runs, the same list-extension convention `"scribe"`/`"remediator"`/
`"report"` already use.

`guardian check-drift`, a new top-level subcommand (sibling to `run` and
`resume` in `cli.py`'s `add_subparsers`), separate from `guardian run`
per point 4:

```
guardian check-drift                        # run the check, write back to DataHub, print a summary
guardian check-drift --incident <id>        # also attach the finding to a saved incident and
                                              # regenerate its audit report with the new section
```

The `--incident` form reuses `orchestrator.load_incident()` (built in
WP3) rather than inventing a second reload mechanism — direct
continuity with the "resume a saved incident from a given stage"
capability decision 0009 already established as a legitimate, named
operation, not a one-off script.

## 4. Explicitly out of scope

- No scheduled or continuous execution — no cron, no background loop.
  `guardian check-drift` only runs when a human (or a CI step a human
  configured) invokes it.
- No alerting or paging. `status: "flagged"` is a field in the output,
  not a trigger for any notification.
- No retraining trigger — there's no training step to trigger; the model
  is a fixed heuristic (§1).
- No drift-over-time dashboard or trend line. DataHub's doc notes and
  assertion run events accumulate a history as a side effect of repeated
  on-demand use (§3.3), but Guardian itself never reads that history back
  or reasons about a trend — only the latest run's `DriftFinding` is
  computed, stored, and shown.

## 5. Honest verdict: no genuine temporal baseline exists

This is the section the repo owner asked to read before any code gets
written. Three independent pieces of evidence, each checked directly
against this repository rather than assumed from convention:

**1. The entire pipeline is deterministically seeded.** `RANDOM_SEED =
42` in `generate_denials.py`, documented in its own comment as "for full
reproducibility across reruns"; `create_db.py` also calls
`random.seed(42)` (though a grep for actual `random.*` usage elsewhere in
that file turns up nothing — raw CSV→SQLite ingestion has zero
randomization to begin with); `seed_upstream_scenario.py` uses its own
distinct fixed seed, `UPSTREAM_SEED`. There is no wall-clock, UUID, or
other entropy source feeding any generated feature value anywhere in the
pipeline (confirmed by grep, not assumption).

**2. `healthcare.db`'s git history is three cumulative build stages, not
independent samples.** `git log --oneline -- src/datahub/healthcare.db`
shows exactly three commits: the original fixture copy, the Sprint 1
build, and the two-scenario seeding addition (decision 0006) — each one
building *on top of* the last, never a fresh independent regeneration at
a second point in time. There is no "Sprint 1 vs. now" pair of
independent samples sitting in history to compare.

**3. New this session, and corrected once mid-investigation — regenerating
`denials`/`denial_model_scores` from the current `claims` data reproduces
it exactly, byte-for-byte, when done correctly; the upstream raw-ingestion
step does not, for a real and different reason.** Point 1 asked
explicitly: *"does regenerating/reseeding change these distributions at
all? verify empirically before assuming yes."* This was tested directly,
against an isolated scratch copy — the real, committed `healthcare.db`
was never modified, confirmed via `git status` throughout both attempts.

The first attempt was wrong, and it's worth showing why rather than
quietly fixing it: `generate_denials.py` + `score_claims.py` were re-run
against the scratch copy WITHOUT first resetting `claims` via
`schema_sprint1.sql`. That's a known, already-documented mistake —
`docs/walkthroughs/sprint-2.md` explicitly warns "do not skip
`schema_sprint1.sql` between reseeds," because `seed_segment_spike()`
reads `claims.billing_amount` as it currently stands rather than
resetting it. Skipping that step produced a stale `claims` table and,
downstream, 49,900 of 55,500 scored claims differing from the committed
db — not a finding about the pipeline, a self-inflicted repeat of a bug
Sprint 2 already caught and documented once.

Redone correctly — `sqlite3 healthcare.db < schema_sprint1.sql`, then
`generate_denials.py`, then `score_claims.py`, the exact sequence
`sprint-2.md` documents — the result is **byte-identical to the committed
database**: 0 of 55,500 `denial_model_scores` rows differ, the denied
`claim_id` set matches exactly, reason-code counts match exactly
(`INVALID_BILLING_AMOUNT: 1788`, `RANDOM_AUDIT: 278`, `HIGH_RISK_SCORE:
278`, both sides). This confirms the fixed-seed argument in point 1 was
right all along, and confirms `sprint-2.md`'s documented reseed procedure
is accurate and safe as written — no fix needed there.

The genuinely non-reproducible layer sits one level further upstream, in
`create_db.py`, and it's a different mechanism entirely: `plant_quality_
issues()` selects which rows get each planted defect via SQLite's own
`ORDER BY RANDOM() LIMIT n` — a separate, C-level PRNG that the adjacent
`random.seed(42)` Python call (and `src/datahub/README.md`'s claim that
"all issues use `random.seed(42)` for reproducibility") has **no effect
on whatsoever**. Confirmed empirically: calling `random.seed(42)`
immediately before each of two `ORDER BY RANDOM()` queries against the
same table, same connection, still produced two different row selections.
Regenerating `healthcare.db` from a freshly downloaded CSV via
`create_db.py` would plant negative-billing/NULL-name/invalid-age/date-
swap defects on a **different random set of rows every time** — with no
error, and no way to reproduce the currently-committed placement even in
principle. This finding, and the fix to `src/datahub/README.md` and
`create_db.py`'s own claim, is documented separately as decision 0010
(regeneration non-determinism) — filed alongside this LLD because it's
the same underlying question ("can we get back to a real second sample
of this data") with a real, more serious answer at that layer.

For §5's purposes this sharpens rather than weakens the "no baseline"
conclusion: not only is there no independent second time-point sample in
this repo's history (point 2), the one layer that's genuinely
non-deterministic (`create_db.py`'s raw-ingestion step) is exactly the
layer you'd have to rerun to manufacture one — and rerunning it wouldn't
recreate the *original* Sprint-1 sample either, since its own placement
was never reproducible to begin with, seed comment notwithstanding.

**Verdict: no genuine "distribution over time" baseline exists, and none
should be manufactured.** §2's design — cap-exceedance against an
already-documented code constant, PSI-shape against a theoretical
distribution, and a range invariant reframed honestly as a
corruption guard rather than a drift signal — is the smallest honest
version, exactly along the lines point 5 itself proposed: "checks
internal consistency of feature distributions against documented
expected ranges," not "true drift over time." No part of the writeback,
CLI help text, or audit report copy should use the word "drift" to imply
a comparison against a real past state; where the WP4/US-6 name itself
needs to appear (command name, ticket reference), it should be paired
with an explicit one-line qualifier saying what it actually checks.

## Alternatives considered

- **Mean/std shift check on `billing_zscore`.** Rejected — §2.1: proven
  tautological (always ≈0/1 by construction of the z-score transform
  itself), both algebraically and against the live data.
- **"Sprint 1 vs. current" distribution comparison.** Rejected — §5: no
  independent second sample exists in history, and the one layer that's
  genuinely non-deterministic (`create_db.py`'s SQL-level `ORDER BY
  RANDOM()`) is exactly the layer you'd need to rerun to manufacture one,
  making that manufacture impossible even in principle, not just
  inconvenient.
- **Treating the range-invariant check as a drift signal.** Rejected —
  §2.2: it's mathematically guaranteed to pass under correct code,
  regardless of the data's real health; reframed honestly as a
  corruption/regression guard instead, the same category as Scribe's
  existing billing-amount assertion.
- **A drift-over-time dashboard / trend view.** Rejected — §4: explicitly
  out of scope per the spec; DataHub's own doc-note/assertion history
  accumulates as a side effect of repeated on-demand runs, but Guardian
  itself doesn't build a view on top of it.
