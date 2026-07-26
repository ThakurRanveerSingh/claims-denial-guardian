# 0008 — Remediator's design: quarantine-not-correction, two validation passes, hybrid PR description

Date: 2026-07-26
Status: Accepted

## Context

Sprint 3 WP2 builds Remediator (US-3) — the last of `hld.md`'s original
four-stage pipeline (Sentinel → Investigator → Remediator → Scribe). Given
an `Incident` with a confident `InvestigatorFinding`, Remediator generates a
fix, validates it deterministically, and opens a real pull request against
the pipeline that produced the defect.

Unlike Scribe (decision 0007), which writes back into DataHub — a system
this project already owns — Remediator has to act on a codebase it does
not own: the real pipeline transform logic. That single fact drives most of
this decision.

## Decision

### 1. Two repos, not one

`~/projects/denial-guardian-data-platform` is a separate, real GitHub repo
(Sprint 3 WP2 Part A) simulating the team that owns the healthcare
pipeline — its `transform/` SQL is derived byte-for-byte from the actual
`healthcare.db` views and `create_db.py`/`schema_sprint1.sql` logic, "so the
repo is honest," not reconstructed from memory.

A single-repo design was rejected. Remediator's whole premise — "generate a
fix and open a pull request for review" — only means something if there's a
genuine boundary being crossed: a PR against your own repo is a self-review
with extra ceremony; a PR against a repo a *different* team owns is the real
scenario this system is built to handle (Guardian's team detects anomalies
in data it consumes, not data it produces). Two repos make that boundary
real instead of simulated-in-comment: a genuine `git clone`, a genuine
`gh pr create` against someone else's `main`, a genuine review surface. It
also keeps the main repo's own git history free of another team's pipeline
SQL — `claims-denial-guardian` stays about Guardian's own agents.

### 2. Quarantine, never correct

**The thesis**, stated once here in full because it's the single most
consequential design call this project has made: the tempting fix for a
sign-flip is `ABS(billing_amount)` — negatives become positive, the anomaly
disappears, the demo looks clean. Remediator never does this. **We don't
actually know the flip's cause, and in health insurance, silently "fixing"
financial data you don't understand is exactly how a data team creates the
next wrongful-denial incident while erasing the evidence of this one.**
Every fix instead builds a dead-letter `<table>_quarantine` table: clean
rows flow, suspect rows get surfaced for a human to review, nothing is
invented. Same evidentiary discipline as Investigator refusing to assume a
root cause it hasn't verified — the product has a consistent character
across all three agents: it never pretends to know more than the evidence
supports. This sentence appears in every PR body Remediator generates
(`_build_pr_body`'s "Why quarantine, not correction" section), not only
here — a reviewer looking at the diff should see the reasoning, not just
the result.

### 3. Fix shape keyed on `primary_root_cause`, not a generic diff engine

Two shapes, both applied to the SAME `denial-guardian-data-platform` repo
but at different files:

- `introduced_at:X` — the defect first appears at X's own build. Fix
  modifies X's transform file directly (`transform/claims.sql`).
- `inherited_from:X` — the defect is already present in the earliest
  available data (X, e.g. `raw_patients`), which this simulated team
  doesn't own and can't fix. Fix instead modifies the first transform stage
  this team DOES own downstream of X (`transform/staging_patients.sql`) —
  same split pattern, one hop later, because the real source can't be
  touched.

Two real, saved incidents (Sprint 2/3) exercise exactly these two shapes —
UnitedHealthcare/diabetes (`introduced_at:claims`) and Cigna/obesity
(`inherited_from:raw_patients`) — and Part D proved they produce genuinely
different diffs, touching different single files each.

### 4. Single-shot LLM generation, not a multi-turn loop

Remediator uses `LLMBackend.complete()` only — no Design A/B split the way
Investigator needs (decision 0004). A real, structural simplification, not
a corner cut: there's no exploration required, just "given this real schema
and this finding, generate SQL," checked deterministically afterward.
`complete()` already works uniformly across all three backends from
Sprint 2, so this needed no new backend machinery.

### 5. Validation: two required deterministic passes (amended from one — see below)

No LLM anywhere in validation — same "LLM proposes, code verifies" split as
Sentinel's z-test and Scribe's idempotency checks.

- **Pass 1** (`src/codegen/sql_validation.py`): the candidate SQL runs
  against a scratch COPY of the real, already-populated `healthcare.db`,
  never the committed file itself (Sprint 3 WP1 Part A's own lesson,
  learned the hard way — an accidental real mutation, caught and reverted).
  Checks zero rows violate the expectation in the clean table, AND
  `clean_count + quarantine_count == original_count` (conservation — catches
  silent row loss, not just violations).
- **Pass 2** (`src/codegen/fresh_build_validation.py`, added after Part D's
  live run — see Amendment below): the ENTIRE transform sequence
  (`staging_patients → mart_billing → mart_demographics → claims`) runs
  against a database seeded with nothing but a small real `raw_patients`
  sample, proving the fix works as a genuine from-scratch build script, not
  merely on top of data an earlier run already built.

Both passes must succeed for an attempt to count. Up to 2 retries (3 total
attempts), with the exact error from whichever pass failed fed back into
the next prompt; after that, an honest failure — no PR, full attempt
history preserved on the returned `RemediatorResult` (evidence-preserving,
same convention as `InvestigatorFinding.evidence`).

### 6. PR description: hybrid, not fully generated and not fully templated

Two options were on the table: let the model write the whole PR
description freely (natural, but re-opens exactly the risk Investigator was
built to avoid — unverified prose describing an unverified fix), or make
the description 100% deterministic template text (safe, but loses the
Investigator's own narrative, which is genuinely worth a reviewer reading).

**Decision: hybrid.** The PR body (`_build_pr_body`) is a deterministic
skeleton built entirely from `Incident`/`InvestigatorFinding` fields —
incident ID, segment, root cause, confidence, the validation tables (both
passes, with real numbers), the operational note, the generated SQL — with
exactly ONE marked section (`## Investigator's Finding (verbatim)`) quoting
`root_cause_summary` in a blockquote, unedited. No fresh LLM prose writes
the description itself; the ONE place free-form model text appears is
clearly labeled as a quote from a different, already-reviewed stage of the
pipeline, not Remediator's own claim.

### 7. Operational note: owner read live from DataHub

A quarantine table without a watcher is a landfill — rerouting bad rows is
only half the pattern, someone has to see them. Cheapest honest close: the
PR body's "Operational note" reports the quarantined row count explicitly
and names a suggested owner, read live via the same MCP `get_entities`
pattern Scribe already established (`ownership.owners[0].owner.name`) —
confirmed live: `claims` → `claims_ops_team`, `staging_patients` →
`clinical_team`. Falls back to an honest "unknown" line, never a guess, if
DataHub has no ownership recorded.

### 8. Distinct, deterministic PR titles per fix shape

`_pr_title` is keyed off `primary_root_cause`'s prefix — e.g. "Guard claims
build: quarantine sign-flipped billing" vs. "Quarantine invalid source
billing at staging boundary" — a formatting decision, not new machinery, so
two PRs sitting side by side (the actual Part D demo) read as visibly
different at a glance.

### 9. Idempotency checked BEFORE any LLM call or DataHub read

A branch name is a pure function of `incident_id` alone
(`_branch_name_for`), so "does a PR already exist for this incident" can be
answered before `fix_target`, schema, or a single token of generation is
needed. An earlier version of this code checked only inside `_open_pr`
(after generation) — moved earlier once it was clear that placement still
prevented a duplicate PR but kept paying for a real LLM call and live
DataHub reads on every rerun, the actually expensive part of a repeat run.

## Alternatives considered

- **`ABS(billing_amount)` / any value-correcting fix.** Rejected — the
  thesis (section 2).
- **One repo instead of two.** Rejected (section 1) — collapses the exact
  cross-team boundary this system exists to act across.
- **Fully LLM-generated PR prose.** Rejected (section 6) — re-introduces
  unverified claims about a fix into the one artifact a human reviewer
  actually reads first.
- **A generic diff/patch engine instead of fix-shape selection.** Rejected
  as speculative generalization — exactly two shapes are demonstrated by
  the two real seeded incidents; building for a third, hypothetical shape
  now would be building against nothing.
- **One validation pass.** This was the ORIGINAL decision, and it was
  wrong — see Amendment.

## Consequences

- `src/agents/remediator.py` needs its own MCP-session-spawning code,
  duplicating `investigator.py`/`scribe.py`'s pattern — same "copied rather
  than shared" convention decision 0007 already established for this kind
  of per-script boilerplate, not revisited here.
- Remediator depends on `denial-guardian-data-platform` existing locally
  (cloned, not re-cloned per run) and on `gh auth status` being valid —
  both real, external preconditions, not abstracted away.
- `Incident` gains a `remediator` field and `"remediator"` joins
  `pipeline_stages_run` when `--remediate` is passed — same extension point
  `lld-sprint2.md` §4.1 anticipated for both Remediator and Scribe.
- `--remediate` is off by default (unlike `--no-writeback`'s default-on/
  opt-out shape) — opening a real pull request is a more visible,
  harder-to-quietly-ignore side effect than a DataHub tag, so the default
  inverts: opt-in, not opt-out.

## Amendment (Part D live run) — a real regression, caught by asking "would I merge it," not by the harness

Part D's first live run produced two real PRs. Both passed Pass 1 (the only
pass that existed at the time) cleanly. Reading them as a human reviewer —
the repo owner's explicit UAT instruction — surfaced a defect neither
automated check had caught: the generated `staging_patients.sql` no longer
had a `CREATE TABLE staging_patients` statement anywhere. It opened with
`DELETE FROM staging_patients`, which requires the table to already exist.
Run against a genuinely fresh database, the file would fail outright.

**Why Pass 1 couldn't see it**: `apply_and_validate_fix` always runs
against a scratch COPY of the real, already-populated `healthcare.db` —
every database it ever tests against already has the table. A fix that
silently stopped being able to *create* the table looked identical, to that
check, to one that always could. The populated-db pass answers "does the
fix clean the data correctly?" The question a reviewer actually asks before
merging a file is different and broader: "does this file still do
everything the old file did?" Fresh-database runnability was a real
invariant of the original files that nobody had written down as a check —
this is the general shape of most escaped bugs: not wrong answers to asked
questions, but unasked ones.

**Fix**: added `src/codegen/fresh_build_validation.py` as a second,
required pass (section 5 above). Re-examining PR #1 (the `claims.sql` fix)
under the new pass showed it had the SAME latent flaw for a different
reason — its `DROP TABLE claims; ALTER TABLE claims_new RENAME TO claims;`
pattern assumed `claims` already existed too, just less obviously (nothing
in Pass 1 exercises a genuinely first-time build of `claims`, so the DROP
never had a chance to fail there). Both PRs were regenerated under the
corrected prompt (an explicit `CREATE TABLE IF NOT EXISTS ... AS SELECT ...
WHERE 0` idiom, worked example included, satisfying fresh-database safety
and the dependent-views constraint below at once) and updated **in place**
on their existing branches (`run_remediator(..., force=True)`, `_open_pr`'s
new `force=` path) — not duplicated into new PRs.

**A second, related bug found the same session**: the ORIGINAL prompt told
the model to `DROP` the table and rebuild it under a temp name before
renaming. Safe for `claims` (nothing else in the database depends on it —
its real downstream, per its own header comment, is Python-computed
`denials`/`denial_model_scores`, not a SQL view). Unsafe for
`staging_patients`, which has real dependent SQL views
(`v_billing_from_staging`, `v_demographics_from_staging`) — SQLite's
`ALTER TABLE RENAME` performs a schema-wide view-consistency pass, and it
failed because for one moment mid-script no table named `staging_patients`
existed. Fixed by changing the prompt's own instruction: never DROP: instead
`CREATE TABLE IF NOT EXISTS` (idempotent, safe on a fresh db), then
`DELETE FROM` + `INSERT INTO` in place — the table object itself never
stops existing at any point in the script.

**A third, unrelated bug found via direct measurement, not inspection**: the
generation call's timeout inherited `llm_backend.py`'s `DEFAULT_COMPLETE_TIMEOUT_S`
(60s), tuned for Sentinel's much lighter narration seam. A live run showed
two of three attempts timing out before any SQL came back at all, even
though a bare sanity `claude -p` call completed in ~3s — the real prompt's
weight (live schema, full current SQL, and on a retry, the previous
attempt's SQL too) needed real headroom. Raised to 240s, matching
`DEFAULT_INVESTIGATE_TIMEOUT_S` — the existing precedent in this codebase
for "a call that does real reasoning," not a guessed number.

**The lesson, worth naming plainly**: the agent validated "does the fix
clean the data?" The repo owner asked "would I merge this file?" — a
different, broader question that includes "does this file still do
everything the old file did?" No check guarded fresh-database runnability
because nobody had written it down as an invariant. UAT's job is asking
that broader question as a human reviewer, not as the test suite — and
doing it caught something the entire automated harness, working exactly as
designed, could not have. Worth noting in the same breath: Remediator's own
behavior that session was the other half of this working correctly — it
reported `status: "failed_validation"` with the real error rather than
rounding an ambiguous result up to success. An agent that states its
reservations instead of claiming PASS is one whose green checkmarks mean
something; the same discipline that made the human review catch something
real is what made the agent's own stated failures trustworthy enough to act
on.
