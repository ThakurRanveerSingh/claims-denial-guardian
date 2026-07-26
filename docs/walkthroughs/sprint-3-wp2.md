# Sprint 3, WP2 — The Remediator (and a real regression caught by UAT)

Built against `docs/decisions/0008-remediator-design.md`, implementing US-3
— the last stage of `hld.md`'s original four-stage pipeline. Five parts,
each reviewed before the next began: **A** (scaffold the simulated
pipeline-owner repo), **B** (design sketch, approved with amendments —
including the project's clearest ethical design call yet: quarantine, not
correction), **C** (implementation), **D** (live proof, two real PRs, and a
real regression the harness missed but human review caught), **E** (this
walkthrough). Done entirely in the main session, no subagent delegation —
the Sprint 3 process change (`docs/token_ledger.md`'s 2026-07-26 WP1 entry)
continued through WP2.

## What was built

| Part | Files | Purpose |
|---|---|---|
| A | `~/projects/denial-guardian-data-platform` (new repo): `transform/*.sql`, `README.md`, `LICENSE` | The simulated pipeline-owner repo — SQL derived byte-for-byte from the real `healthcare.db`, so Remediator's PRs land against something real, not a fiction |
| B | `docs/decisions/0008-remediator-design.md` | Fix-shape selection, quarantine-not-correction, hybrid PR description, two-repo rationale |
| C | `src/codegen/sql_validation.py`, `src/agents/remediator.py`, `tests/test_remediator.py`, `tests/test_sql_validation.py` | Deterministic populated-db validation + the Remediator module itself: LLM generation, retry loop, git/gh mechanics, idempotent PR detection |
| C | `src/agents/orchestrator.py`, `src/agents/cli.py`, `tests/test_orchestrator.py` | Wired in as the stage after Scribe; `--remediate` to enable (off by default) |
| D | Two real PRs on `denial-guardian-data-platform`; `examples/<incident-id>/fix/` in this repo | Live proof against both real saved incidents |
| D (regression) | `src/codegen/fresh_build_validation.py`, `tests/test_fresh_build_validation.py`, prompt + `_open_pr`/`run_remediator` changes | A second, required validation pass — added after UAT caught what the first pass couldn't see |

Test suite: **266 tests total**, 4 marked `@pytest.mark.live` (excluded by
default).

## Part D, first pass: two real PRs, one real hesitation

Ran Remediator against both real saved incidents:

- **UnitedHealthcare/diabetes** (`introduced_at:claims`) → PR against
  `transform/claims.sql` only. Succeeded on the first attempt.
- **Cigna/obesity** (`inherited_from:raw_patients`) → PR against
  `transform/staging_patients.sql` only. Failed twice before succeeding —
  the first real bugs found live: a 60-second generation timeout too short
  for the real prompt's weight, and a `DROP TABLE` + rebuild pattern that
  broke `staging_patients`'s real dependent SQL views
  (`v_billing_from_staging`, `v_demographics_from_staging`) specifically —
  `claims` has no such dependents, which is why its own fix never hit this.

Both PRs' diffs were correctly scoped (one file each), their body numbers
matched each incident's `incident.json` exactly, and the conservation check
showed real PASS results with real counts. Reading them as a human
reviewer — the repo owner's explicit instruction, going beyond what any
automated check asserts — surfaced something the harness missed entirely:
the Cigna fix's `staging_patients.sql` no longer had a `CREATE TABLE`
statement anywhere. It would fail outright against a genuinely fresh
database. That hesitation was the finding.

## The regression, root-caused

`apply_and_validate_fix` (Pass 1, the only pass that existed at Part D's
first run) always runs a candidate fix against a scratch COPY of the real,
already-populated `healthcare.db` — every database it ever tests against
already has the table in question. A fix that silently stopped being able
to *create* that table looked identical, to that one check, to a fix that
always could. Pass 1 answers "does this fix clean the data correctly?" The
question a reviewer asks before merging a file is broader: "does this file
still do everything the old file did?" Fresh-database runnability was a
real invariant of the original files that nobody had written down as a
check — the general shape of most escaped bugs: not wrong answers to asked
questions, but unasked ones.

Built `src/codegen/fresh_build_validation.py` as a second, required pass:
seeds a genuinely empty database with nothing but a small REAL sample of
`raw_patients` rows (pulled live, not fabricated), then runs the entire
transform sequence — `staging_patients → mart_billing → mart_demographics →
claims` — with the candidate SQL standing in for its one stage and every
other stage running its real, currently-committed file. Re-checking PR #1
(`claims.sql`, already merged-clean by Pass 1) under this new pass showed it
had the identical latent flaw for a different reason: its own
`DROP TABLE claims; ALTER TABLE claims_new RENAME TO claims;` pattern
assumed `claims` already existed too — Pass 1 never exercised a genuine
first-time build of `claims`, so the DROP never had a chance to fail there.

Fixed the generation prompt with a concrete, worked `CREATE TABLE IF NOT
EXISTS ... AS SELECT ... WHERE 0` idiom (satisfies fresh-database safety
and the dependent-views constraint at once — the table object never stops
existing, and a from-scratch database gets the right empty schema for
free), plus an explicit instruction to preserve the original file's
design-rationale header comments rather than replace them. Added
`force=True` support to `run_remediator`/`_open_pr` to regenerate and push
an update onto an ALREADY-OPEN PR's branch in place, rather than only being
able to open a fresh one — needed to correct the two PRs Part D's first run
had already produced, without duplicating them.

## Part D, second pass — the real re-UAT

Regenerated both PRs with `force=True`. Both succeeded on the first
attempt this time, both passes green, both updated **in place** on their
original branches (same PR URLs, no duplicates):

- **PR #1**: https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/1 — `claims.sql` only. `CREATE TABLE IF NOT EXISTS` for both `claims` and `claims_quarantine`, no `DROP`. Original header comment (the rowid-vs-name+hospital join rationale) fully preserved, the fix's own reasoning appended underneath, citing the real investigation numbers (325 `introduced_at:claims`, 36 `inherited_from:raw_patients`). Conservation: 55500 → 54037 clean + 1463 quarantined, PASS. Fresh-database build: PASS.
- **PR #2**: https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/2 — `staging_patients.sql` only. Same pattern, same conservation numbers (55500 → 54037 + 1463 — the same 1463 rows negative at every pipeline stage, since `staging_patients` passes `billing_amount` through unchanged by design, a genuine cross-check the two incidents happened to surface together). Original header preserved, fix rationale appended citing the real numbers (280/298 denials, 100% reproduction at every hop). Fresh-database build: PASS.

Both earned a clean, unreserved "I'd merge this" on the second read.

## The lesson

The agent validated "does the fix clean the data?" The question that
actually matters before merging a file is "would I merge this?" — which
includes "does this file still do everything the old file did?" No check
guarded fresh-database runnability because nobody had written it down as an
invariant; UAT's job is asking that broader question as a human reviewer,
not as the test suite, and doing it caught something the automated harness
— working exactly as designed — could not have. The other half of this
working correctly was the agent's own behavior: Remediator reported
`status: "failed_validation"` with the real error on every genuine failure,
never rounding an ambiguous result up to success. An agent that states its
reservations instead of claiming PASS is one whose green checkmarks mean
something.

## Process note

Two smaller, real bugs were also found and fixed live this session,
unrelated to the regression above: `_existing_pr_url` used `gh ... --jq
'.[0].url'`, which returns the literal string `"null"` (not empty output)
when no PR exists — would have broken idempotency detection in exactly the
case it exists to handle. Fixed by parsing the JSON array directly instead
of trusting jq's null-propagation behavior on an empty result.
