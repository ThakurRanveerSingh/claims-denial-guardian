# 0006 — Two seeded scenarios: direct-injection (kept) plus a genuinely upstream contrast case

Date: 2026-07-24
Status: Accepted

## Context

`lld-sprint2.md` §0-§2 (committed as `a188ba7`) designed Investigator around
one seeded incident: `UnitedHealthcare`/`diabetes`, where
`generate_denials.py`'s `seed_segment_spike()` mutates `claims.billing_amount`
directly, after `claims` is already populated. The empirical finding that
shaped that design — 325 of 361 flagged claims (90%) have no matching defect
anywhere upstream, only 36 (10%) do — is real and correctly designed for. But
it only demonstrates one *direction* of Investigator's hypothesis-testing
logic (§2.2 step 4: "does this reproduce at the immediate upstream source?").
US-2's lineage-walk story and US-3's (Remediator, later sprint) fix-generation
story both need at least one incident where the answer is genuinely "yes,
all the way to `raw_patients`" — real upstream remediation required, not
just at `claims`. Reviewed and approved by the repo owner with this
amendment: add a second scenario, keep the first unchanged as the contrast.

## Decision

**Add a second seeded incident — `Cigna`/`obesity` — whose negative billing
values are injected directly into `raw_patients`, `staging_patients`, and
`mart_billing` (all three, same rowids, same values), before `claims` is
(re)built from them.** The existing `UnitedHealthcare`/`diabetes` scenario
is kept exactly as designed, unchanged, specifically *because* it's a
different injection mechanism (post-population, directly into `claims`)
producing a different (downstream-origin) finding — that contrast is the
whole point.

Concrete parameters (full reasoning and verified z-scores in
`lld-sprint2.md` §10):

| | Existing (unchanged) | New |
|---|---|---|
| Segment | `UnitedHealthcare` / `diabetes` | `Cigna` / `obesity` |
| Mechanism | direct injection into `claims`, post-population | injection into `raw_patients` + `staging_patients` + `mart_billing`, pre-population |
| Seed | `RANDOM_SEED = 42` | `UPSTREAM_SEED = 43` |
| Target rate | 20% | 15% |
| Verified z-score (both scenarios composed) | 35.53 | 25.69 |
| Verified root cause | `introduced_at:claims` (90%) / `inherited_from:mart_billing` (10%) | `inherited_from:raw_patients` (100%) |

No new `denial_reason_code` is introduced — confirmed, not assumed
(`lld-sprint2.md` §10.2): rule 1 (`INVALID_BILLING_AMOUNT` for any claim
with `billing_amount < 0`) already doesn't care where the negative value
originated, so the new scenario reuses it as-is.

## Alternatives considered

- **Inject only into `mart_billing`, skip `raw_patients`/`staging_patients`.**
  Rejected. `mart_billing.billing_amount` alone reproducing, with
  `staging_patients`/`raw_patients` clean, is a real, different finding
  (`"introduced_at:mart_billing"`) — a third kind of result, not the
  "genuinely upstream, traceable to `raw_patients`" contrast case the
  amendment asked for. Verified this session that `raw_patients.rowid`,
  `staging_patients.rowid`, and `mart_billing.rowid` all refer to the same
  logical row for all 55,500 rows (read-only query against the real,
  committed database — see `lld-sprint2.md` §10.2), which is what makes
  writing the same value at the same rowid into all three tables tractable
  and unambiguous.
- **Re-run `create_db.py` from the original Kaggle CSVs, with the new
  segment's rows pre-negated, to regenerate the whole database from
  scratch.** Rejected — heavier than necessary (requires the source CSVs,
  which may not be present; re-derives 55,500 rows across four tables from
  scratch) for a targeted, single-segment change. A direct `UPDATE` against
  the three already-existing upstream tables, using the same
  reproducible-seed technique `seed_segment_spike()` already uses, achieves
  the identical *result* (negative values genuinely present at every
  upstream hop) without rebuilding anything that doesn't need rebuilding.
- **Reuse the existing scenario's seed (`42`) and/or target rate (20%) for
  the new scenario.** Rejected — not a correctness issue (the two
  `rng.sample()` calls draw from disjoint populations, so no actual
  collision), but a distinct seed (`43`) and rate (`15%`) make it visibly a
  separate, deliberately chosen parameter rather than an accidental
  copy-paste, matching `generate_denials.py`'s own convention of naming
  tunables explicitly.
- **Pick a new segment sharing a provider or condition with the existing
  one** (e.g. `Cigna`/`diabetes`, or `UnitedHealthcare`/`obesity`).
  Rejected — risks reading, in a live demo, as "this provider/condition is
  systematically bad" rather than two genuinely distinct, unrelated
  incidents, which would blur the exact discrimination the second scenario
  exists to demonstrate. `Cigna`/`obesity` shares neither dimension with
  `UnitedHealthcare`/`diabetes`.
- **Unify both scenarios under one injection mechanism** (e.g. rewrite the
  existing scenario to also inject upstream, so both incidents use the same
  code path). Rejected — explicitly counter to the point of this amendment.
  The two scenarios are valuable specifically *because* they use different
  mechanisms and land at different root-cause classifications
  (`introduced_at:claims` vs. `inherited_from:raw_patients`); collapsing
  them into one mechanism would mean Investigator's design could no longer
  be shown to discriminate between the two answers.
- **Mutate the real, committed `healthcare.db` directly this session to
  produce the verified numbers.** Rejected — explicit hard constraint on
  this task (it's a tracked file per decision 0002; the generator itself is
  Slice 0, separate future implementation work). Instead: copied the file
  to an isolated scratchpad location, ran the actual unmodified
  `schema_sprint1.sql`/`generate_denials.py`/`score_claims.py` against the
  copy alongside a simulation-only draft injection script, computed the
  numbers there, and deleted the copy. Confirmed via `git status`/`git diff
  --stat` that the real repo file is byte-identical to before this session.

## Consequences

- `lld-sprint2.md` gains an addendum (§10), appended after the already-
  committed §0-§9 rather than editing them — the single-scenario design
  they document remains an accurate historical record of what was designed
  and reviewed at the time.
- Slice 0's real implementation now has two concrete, fully-specified
  targets to build against (segment, mechanism, seed, rate for each
  scenario) instead of one, plus a documented, verified sequencing
  requirement: the new upstream-injection script must run *before*
  `schema_sprint1.sql` rebuilds `claims`, or its effect is silently
  invisible to the rest of the pipeline until the next rebuild.
- `InvestigatorFinding`'s contract (`lld-sprint2.md` §2.3) needed its
  *example* extended to show a 100%-upstream case, not its *shape* changed
  — confirmed rather than assumed, closing a real gap where the only
  worked example happened to be the minority-upstream case.
- A real, checked (not assumed) side effect is now documented: with two
  simultaneous real incidents in the same dataset, each one's leave-one-out
  z-score baseline shifts slightly because of the other (`UnitedHealthcare`/
  `diabetes` moved from z=38.00 single-scenario to z=35.53 composed) — noted
  in `lld-sprint2.md` §10.6 so it isn't mistaken for an error if a future
  sprint adds a third scenario and sees the same kind of shift again.
