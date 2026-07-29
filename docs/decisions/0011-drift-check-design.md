# 0011 — Drift/feature-health check design (US-6)

Date: 2026-07-28
Status: Accepted

## Context

Sprint 3 WP4 builds a single, on-demand feature-health check against
`denial_risk_model` — intentionally minimal per the original Sprint 3
brief ("one drift signal, not a monitoring suite"). Design work happened
first, as a standalone LLD (`docs/architecture/lld-sprint3-wp4.md`) rather
than the in-chat sketch WP1–3's Part A used, because the repo owner
explicitly asked to review the honesty verdict on whether a real baseline
exists before any code was written. This decision doc records what was
actually built, once real behavior existed to document — the same
close-out pattern 0007/0008/0009 already established.

## Decision

### 1. "Drift" is redefined honestly — internal consistency, not change over time

The LLD's own investigation (§5, later corrected and strengthened — see
decision 0010) established that no genuine "baseline vs. current" temporal
comparison is possible with this project's dataset: the pipeline is fully
deterministic (fixed seeds throughout, confirmed empirically that the
`generate_denials.py`/`score_claims.py` layer reproduces the committed db
byte-for-byte when the documented rebuild sequence is followed), the git
history is cumulative build stages not independent samples, and the one
genuinely non-deterministic layer (`create_db.py`'s `ORDER BY RANDOM()`)
is exactly the layer that would need to be rerun to manufacture a second
time point — meaning even that path can't produce one.

Built instead: single-snapshot checks of whether the model's CURRENT input
data still satisfies its own documented mathematical properties. Nowhere
in the code, CLI help text, or report copy does "drift" imply a
comparison against a real past state — `DriftFinding`'s own module
docstring states this explicitly, and the audit report's Model Health
Check narrative repeats it in plain English every time the section
renders.

### 2. The mean/std check is rejected as tautological — proven, not assumed

`billing_zscore` is z-scored per-segment using `statistics.pstdev`
(population, divide-by-n). This makes the pooled mean and variance across
ALL claims exactly 0 and 1 by algebraic construction — not an empirical
property of the data, a guaranteed identity of the transform itself.
Verified twice: algebraically (sum of per-segment zero-sum deviations is
zero; sum of per-segment `n·pstdev²` terms equals total claim count under
population variance) and against the live data (pooled mean measured at
`-1.1e-17`, pooled std at exactly `1.0`). A check built on this would pass
identically whether the pipeline is healthy or badly broken — the
"whichever sounds more impressive, not whichever fits honestly" trap the
original spec explicitly warned against.

Built instead, both genuinely non-circular:
- **Cap exceedance** — the fraction of claims whose raw `billing_zscore`
  exceeds `score_claims.py`'s own documented `BILLING_ZSCORE_CAP` (4.0).
  Reuses a boundary the model's code already committed to rather than
  inventing a threshold. No flag status is attached to this number in v1
  — reported plainly for visibility (currently 0.40% of claims); adding a
  threshold here would itself be an invented number dressed up as
  meaningful.
- **PSI shape check** — the observed distribution's Population Stability
  Index against the theoretical standard normal it's supposed to
  approximate (10 deciles, published <0.10/0.10–0.25/>0.25 convention,
  not project-invented). Z-scoring fixes mean/variance but not shape, so
  this is a real, non-tautological single-snapshot signal (currently
  0.038, healthy).

### 3. `segment_denial_rate`'s range check is a corruption guard, not a drift signal

`denial_rate = denied_count / len(rows)` is bounded to `[0,1]` by
construction of the counting itself — also a mathematical identity, not
an empirical fact. Kept anyway, but explicitly reframed: this check will
always pass while the pipeline's arithmetic is intact, and its value is
catching a REGRESSION (a bug, corrupted data) if it ever fails — the same
category as Scribe's existing `billing_amount >= 0` assertion, not a
distributional-change detector. The plain-English summary states this
distinction directly rather than leaving it implicit.

### 4. Feature list read from DataHub, check implementations stay in code

`run_drift_check()` reads the live `denial_risk_features` MLFeatureTable
via MCP (decision 0003) and dispatches on the returned feature names
against a small `FEATURE_CHECKS` table — the SET of features to check
comes from DataHub, not a Python literal, per the LLD's explicit "don't
hardcode" instruction. The check IMPLEMENTATIONS necessarily stay in
code (DataHub can say a feature named `X` exists; it can't say what a
sane range for `X` is) — same boundary Reporter's `severity_for()`
thresholds already accept. If DataHub ever lists a feature with no
matching entry, the finding reports it explicitly as `"unimplemented"`
and `"flagged"` rather than silently omitting it — the same
quarantine-not-hide discipline decision 0008 established for Remediator.

### 5. Writeback: tag/doc-note on the MLModel, assertions on the dataset — a real, live-caught correction

The original design (per the LLD) proposed writing tag, doc-note, AND
assertions all onto `denial_risk_model` (the MLModel entity), extending
Scribe's pattern directly. Live-testing this immediately surfaced a real
422 from DataHub: `CustomAssertionInfo.entity` must be a dataset-typed
URN ("Required: [dataset]"), and `.field` must be a real schemaField URN,
not a bare string — an MLModel can't be a native assertion target at all
in this DataHub version.

Fixed by resolving `denial_model_scores` (the real dataset holding the
actual feature values, confirmed live: `urn:li:dataset:(urn:li:
dataPlatform:sqlite,healthcare.main.denial_model_scores,PROD)`) via MCP
search — same `_resolve_entity_urn`-style live lookup Scribe already
uses, not hardcoded — and building real schemaField URNs from its
`segment_denial_rate_feature`/`billing_zscore_feature` columns. This is
arguably the more correct design anyway: assertions are fundamentally a
dataset-quality concept in DataHub, and `denial_model_scores` is
literally where the checked values live. The tag and documentation note
stay on `denial_risk_model` itself — no dataset-only constraint applies
to `GlobalTags`/`institutionalMemory`, and the model entity is still the
right place for a human browsing DataHub to see "has this been checked."

One assertion per FEATURE (`denial_guardian_segment_denial_rate_health`,
`denial_guardian_billing_zscore_health`), not per individual check —
`billing_zscore`'s cap-exceedance and shape checks share one assertion,
since DataHub's assertion granularity is naturally per-(dataset,
property), and both checks are about the same underlying column.

### 6. Idempotency: tag/assertion-definition by fixed URN, doc notes by `check_id`

Same three-tier discipline Scribe already established (decision 0007),
applied here: the tag and each assertion's DEFINITION are idempotent by
membership/existence check against fixed, reusable URNs — re-running
`guardian check-drift` never re-applies or re-defines them. Doc notes are
keyed by `check_id` (`f"drift-{timestamp}"`, same convention `incident_id`
uses), so re-running the exact same finding is a no-op, but a genuinely
new invocation appends a new note — giving DataHub's own UI a real,
growing audit trail of checks over time as a side effect of repeated
on-demand use, without Guardian itself building any monitoring/scheduling
on top of it (explicitly out of scope, LLD §4). Assertion RUN EVENTS are
still emitted every time (timeseries, not idempotent state — matching
Scribe's own assertion run events). Verified live, not assumed: writeback
called twice with the identical `DriftFinding` object correctly reported
`tag_already_present`/`doc_note_already_present`/`assertion_already_
defined` on the second call, with the assertion run event still emitted
both times.

### 7. `attach_drift_finding()` composes with `load_incident()`/`write_incident()`, not a new mechanism

`guardian check-drift --incident <id>` reuses the exact reload
infrastructure WP3 built for `resume_incident()` — `load_incident()` to
reconstruct the saved `Incident`, `write_incident()` to re-save it,
`write_audit_reports()` to regenerate the report. Deliberately NOT folded
into `resume_incident()`'s own `stage=` dispatch: both of that function's
existing stages consume `incident.investigator` to do real work; a drift
check is model-level and doesn't read anything from the incident at all
— it's attached to one purely so the report can show it alongside a real
investigation. Treating it as a same-shape-different-mechanism operation
would blur that distinction for no real benefit.

### 8. Model Health Check section follows decision 0009's leak-safety discipline exactly

Plain-English `plain_summary` strings (written compliance-reader-safe at
the source — zero function names, file paths, or raw thresholds) render
in the main narrative; `documented_expected` (which deliberately DOES
carry a code reference, e.g. `"score_claims.py"`) and the full raw
metric table live in the Technical Appendix only. This was checked
directly, not assumed safe by association with the already-sanitized
narrative fields — a regression test (`TestModelHealthSection` in
`tests/test_reporter.py`) asserts `documented_expected`'s code references
and the raw `check_id` are absent from the main body and present in the
appendix, for both real incidents with a fabricated finding attached.

## Alternatives considered

- **Mean/std shift check for `billing_zscore`.** Rejected — section 2:
  proven tautological both algebraically and against live data.
- **Hard-coding a cap-exceedance flag threshold.** Rejected — section 2:
  no such threshold is documented anywhere in this project; inventing one
  would be exactly the "sounds more impressive, not honest" trap.
- **Treating the range-invariant check as a drift signal.** Rejected —
  section 3: it's a mathematical guarantee under correct code, reframed
  as a corruption guard instead.
- **Assertions on `denial_risk_model` (the MLModel) directly.** Rejected
  after a live 422 proved it impossible — section 5.
- **A drift-over-time dashboard or trend view.** Rejected — explicitly
  out of scope (LLD §4); DataHub's own doc-note/assertion history
  accumulates as a side effect of repeated on-demand runs, but Guardian
  itself never reads that history back.
- **Folding `--incident` attachment into `resume_incident()`'s stage
  dispatch.** Rejected — section 7: different shape of operation (model-
  level vs. incident-stage), blurring it would cost clarity for no gain.

## Consequences

- `src/agents/drift.py` is a new, standalone module — zero LLM calls,
  same "LLM proposes (nowhere here), code verifies" boundary every other
  deterministic stage in this codebase keeps.
- `Incident.drift` follows the exact `scribe`/`remediator` optional-field
  convention; `pipeline_stages_run` gains `"drift"` the same way
  `"scribe"`/`"remediator"`/`"report"` already do.
- Both canonical demo incidents now carry a real, live-computed
  `DriftFinding` (attached via the actual `guardian check-drift
  --incident <id>` CLI, not backfilled by hand) — their audit reports'
  Model Health Check sections show real numbers, not placeholder text.
- A real, live-caught bug (section 5) was found and fixed before this
  closed out, not left for a future session to discover the hard way —
  same "verify against the live system" discipline this project has
  applied throughout, this time catching a real DataHub schema
  constraint neither the LLD nor the mocked test suite could have
  surfaced on their own.

## Upstream issue filed (Sprint 4 WP2)

The 422 this decision's §5 documents was filed upstream against DataHub
itself: [datahub-project/datahub#18743](https://github.com/datahub-project/datahub/issues/18743)
— not disputing that assertions may be intentionally dataset-scoped only,
just that `CustomAssertionInfo`'s error messages ("Required: [dataset]"
for a non-Dataset `.entity`; "Failed to retrieve entity with urn
segment_denial_rate, invalid urn" for a bare-string `.field`) don't name
the actual constraint, costing real debugging time to trace back to "must
be a Dataset URN" / "must be a schemaField URN" respectively. Confirmed
against a live `datahub docker quickstart` instance (server v1.5.0.6,
`acryl-datahub` SDK 1.6.0.15).
