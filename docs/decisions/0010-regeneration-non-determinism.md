# 0010 — Regeneration instructions are non-deterministic; the committed `healthcare.db` is canonical, not reproducible-on-demand

Date: 2026-07-27
Status: Accepted

## Context

While writing Sprint 3 WP4's drift-check LLD (`docs/architecture/lld-
sprint3-wp4.md`), point 1 of that spec required verifying — empirically,
not by assumption — whether regenerating this project's data changes its
distributions. That investigation surfaced a real, separate issue: this
repo's own setup docs contain an instruction that, if followed, silently
produces a different `healthcare.db` than the one every currently-
committed artifact (incidents, PRs, audit reports, z-scores) was computed
against — exactly the kind of trap a sharp judge running this project's
own documented steps could fall into. Flagged and fixed this session,
rather than left for someone else to discover by getting a different
answer than the one committed in `examples/`.

This doc also corrects a mistake made earlier in the same investigation:
an initial test wrongly concluded the `generate_denials.py`/
`score_claims.py` layer was itself non-deterministic. It isn't — the test
was wrong, not the pipeline. See §2.

## Decision

### 1. `create_db.py`'s planted-issue placement is not actually reproducible, despite its own documentation saying it is

`create_db.py` calls `random.seed(42)` once, near the top of the file.
Its docstring and `src/datahub/README.md` (line 138, prior to this
session's fix) both state "all issues use `random.seed(42)` for
reproducibility." This is false for the mechanism actually used.

`plant_quality_issues()` selects which rows receive each of the four
planted defects (negative billing, NULL names, invalid ages, date swaps)
via SQLite's own `ORDER BY RANDOM() LIMIT n` — a C-level PRNG internal to
SQLite, entirely separate from Python's `random` module. Python-level
seeding has no effect on it. Confirmed directly, not inferred from
reading the code:

```
$ python3 -c "
import sqlite3, random
conn = sqlite3.connect('healthcare.db')
random.seed(42); r1 = conn.execute('SELECT rowid FROM raw_patients ORDER BY RANDOM() LIMIT 5').fetchall()
random.seed(42); r2 = conn.execute('SELECT rowid FROM raw_patients ORDER BY RANDOM() LIMIT 5').fetchall()
print(r1 == r2)"
False
```

Identical Python-level seeding, immediately before each of two identical
queries, on the same connection — still two different results. Running
`create_db.py` against a freshly downloaded copy of the source CSV would
plant all four defects on a **different random set of rows** than the
currently-committed `healthcare.db` has, with no error or warning, and no
way to recover the original placement afterward.

### 2. Correction: the `generate_denials.py` / `score_claims.py` layer IS deterministic — an earlier test in this same session said otherwise, and was wrong

The WP4 LLD's first draft of its §5 claimed regenerating denials/scores
against the current `claims` table produced 90% different results even
with the fixed seed. That test skipped a required step: resetting
`claims` from `mart_billing` via `schema_sprint1.sql` before re-running
`generate_denials.py`. This is a **known, already-documented** mistake —
`docs/walkthroughs/sprint-2.md` states plainly: "Do not skip `schema_
sprint1.sql` between reseeds," because `seed_segment_spike()` reads
`claims.billing_amount` as it currently stands rather than resetting it
itself. The first test reproduced that already-catalogued bug against
itself, not a new finding about the pipeline.

Redone correctly — `sqlite3 healthcare.db < schema_sprint1.sql`, then
`generate_denials.py`, then `score_claims.py`, the exact sequence
`sprint-2.md` documents — against an isolated scratch copy (the real,
committed database was never touched; confirmed via `git status` before
and after both attempts): **0 of 55,500 rows differ** from the committed
`denial_model_scores` table. Denied claim-ID set, reason-code counts, and
every `segment_denial_rate`/`billing_zscore` value matched exactly.

This matters beyond correcting the record: it confirms `sprint-2.md`'s
existing reseed instructions are accurate and safe as documented — no fix
needed there — and it isolates the real non-determinism to exactly one
place: `create_db.py`'s raw-ingestion step (§1), not the layers built on
top of it.

### 3. Mitigation: the committed database is canonical; fix the instruction, don't fix the RNG

Two options were on the table: (a) correct the documentation to say
"use the committed db, don't regenerate," or (b) if regeneration were
genuinely required for judges to run this project, treat the non-
determinism itself as a bug to fix in `create_db.py`.

**(a) was correct, not (b).** Nothing in this project's actual working
path — `guardian run`, `guardian resume`, the pytest suite, every
walkthrough from Sprint 1 through Sprint 3 WP3 — ever calls `create_db.py`.
Decision 0002 already established the committed-db-as-canonical
philosophy when the fixture was first copied into this repo ("keeps the
demo fully self-contained and reproducible for anyone who clones the
repo"); decision 0006 independently rejected re-running `create_db.py`
from scratch as "heavier than necessary... requires the source CSVs,
which may not be present." `create_db.py` has never actually been the
setup path for this project — it's provenance history for a fixture that
predates it (`lld-sprint1.md` even labels its copy "kept for provenance/
regeneration reference"). The bug was that `src/datahub/README.md` —
inherited near-verbatim from that original, project-independent fixture
— still read as if regeneration were a normal, supported thing to do,
including a specific, false reproducibility claim.

Fixing the RNG itself (option b) was rejected even though it's
technically feasible (e.g., seeding via a deterministic row-selection
method instead of `ORDER BY RANDOM()`): doing so would only make *future*
regenerations reproducible — it does nothing to make a regeneration
reproduce the *current*, already-committed database, since that
database's own placement was never derived from a reproducible process in
the first place. A fix here would risk exactly what point 1 of the WP4
spec was worried about: "fixing determinism now could change the very
z-scores every artifact/PR/report currently shows" — except the risk
would be self-inflicted (nobody needs to regenerate), for a code path
nothing in this project actually depends on.

`src/datahub/README.md` fixed this session:
- The false "all issues use `random.seed(42)` for reproducibility" claim
  replaced with the corrected mechanism and a pointer to this doc.
- The "Generate from Scratch" section relabeled "don't, for this
  project," explicitly stating the committed db is canonical, with the
  one-line reason, and the original steps kept but marked historical-
  reference-only, not a setup instruction.
- The "All Available Commands" listing's `create_db.py` line annotated
  with the same caveat, so it can't be read as an implied normal command
  in isolation from the section above it.

## Alternatives considered

- **Fix `create_db.py` to make `ORDER BY RANDOM()` actually reproducible**
  (e.g., seed a row-index list in Python and select by it instead of
  delegating to SQLite's PRNG). Rejected — §3: doesn't reproduce the
  *already-committed* database, which is the one every artifact in this
  repo depends on; fixes a code path this project never actually
  exercises, at the cost of implying regeneration is now a safe, supported
  thing to try.
- **Leave `README.md`'s reproducibility claim as-is, since in practice
  nobody has run `create_db.py` since Sprint 1.** Rejected — the entire
  reason this was investigated is that a false, committed claim of
  reproducibility is precisely the kind of thing a careful outside reader
  (a judge auditing the project's own documentation) could act on and get
  burned by, whether or not anyone on this team has hit it yet.
- **Delete `create_db.py` and the "Generate from Scratch" section
  entirely**, rather than relabel them historical. Rejected — decision
  0002 already chose to keep the original fixture's provenance intact
  ("kept for provenance/regeneration reference"); removing it would lose
  genuinely useful history (how the planted issues were originally built)
  for a problem that relabeling and warning already solves.

## Consequences

- `src/datahub/README.md` no longer claims a reproducibility property
  `create_db.py` doesn't have, and no longer reads as a normal setup step
  that includes regeneration.
- `docs/architecture/lld-sprint3-wp4.md` §5 corrected in place: the
  `generate_denials.py`/`score_claims.py` layer is confirmed genuinely
  deterministic (0/55,500 mismatches under the documented reseed
  sequence); the real non-determinism is isolated to `create_db.py`'s
  `ORDER BY RANDOM()` usage, which if anything strengthens WP4's "no
  genuine baseline exists" verdict rather than weakening it — the one
  layer that could theoretically produce a second sample is exactly the
  layer that was never reproducible to begin with.
- No code changed in `create_db.py`, `generate_denials.py`, or
  `score_claims.py` — this is a documentation-only fix. The committed
  `healthcare.db` and every artifact computed against it remain valid and
  unchanged.
