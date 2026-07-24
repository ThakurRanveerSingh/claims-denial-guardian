# Sprint 1, Day 1 — claims/denials layer, toy risk model, DataHub registration

Built against `docs/architecture/lld-sprint1.md`, on the copy of the healthcare
fixture at `src/datahub/healthcare.db` (decision 0002).

## What was built

| File | Purpose |
|---|---|
| `src/datahub/schema_sprint1.sql` | DDL for `claims`, `denials`, `denial_model_scores`; populates `claims` via a deterministic join |
| `src/datahub/generate_denials.py` | Seeds the `denials` table: negative-billing rule, background baseline, and the segment spike |
| `src/datahub/score_claims.py` | Toy denial-risk heuristic; scores every claim into `denial_model_scores` |
| `src/datahub/add_lineage.py` (extended) | 3 new lineage edges: `claims ← mart_billing, mart_demographics`; `denials ← claims`; `denial_model_scores ← claims` |
| `src/datahub/add_metadata.py` (extended) | New tags on the 3 tables, 2 new glossary terms, new `claims_ops_team` ownership group |
| `tests/test_sprint1.py` | 12 pytest checks: schema, row counts, anomaly, scores |

All three data-generation scripts were run against DataHub (`localhost:8080`) via
`datahub ingest -c ingest.yaml` → `add_lineage.py` → `add_metadata.py`, all
three completing without errors.

## Results

- `claims`: 55,500 rows (matches `mart_billing` exactly — one claim per admission record).
- `denials`: 2,096 rows (3.8% of claims) — 1,540 `INVALID_BILLING_AMOUNT`, 278 `RANDOM_AUDIT`, 278 `HIGH_RISK_SCORE`.
- **Seeded anomaly**: `UnitedHealthcare` × `diabetes` sits at a **20.5% denial rate**, vs. the next-highest segment at 4.1% — a clear, unmissable outlier against a baseline of ~2-4% everywhere else.
- **Model**: all 55,500 claims scored (`risk_score` range 0.0141–0.4243); the 5 highest-scored claims in the *entire dataset* are exactly the seeded negative-billing claims.
- All 12 tests in `tests/test_sprint1.py` pass.
- DataHub: 8 tables registered (`claims`, `denials`, `denial_model_scores` + the original 4 + `sqlite_sequence`, see below), 9 lineage edges, 5 tags, 5 glossary terms, 4 ownership groups.

## Trade-offs and decisions made along the way

**Idempotent-by-design SQL/scripts, not "run once and never touch again."** `schema_sprint1.sql` does `DROP TABLE IF EXISTS` + `CREATE TABLE` rather than guarding against re-runs, and both `generate_denials.py`/`score_claims.py` clear their own table before repopulating. This is derived data, not source data — rebuilding it from scratch on every run made iterating through two real bugs (below) painless instead of requiring manual cleanup each time.

**`claim_id` derived from `mart_billing.rowid`, not a UUID.** Traceable straight back to the source row (`CLM-014293` → rowid 14293), deterministic across reruns — useful for Investigator's future narrative, and free given the fixed seed everywhere else in this fixture.

**`medical_condition` joined via `rowid`, not `name`+`hospital`.** The LLD measured a ~10.7% collision rate on the name+hospital join; `rowid` is a guaranteed 1:1 correspondence since both marts derive from `staging_patients` with no filtering or aggregation.

**Spike segment: `UnitedHealthcare` × `diabetes`, target rate 20%.** Both are implementation-time parameters the LLD deliberately left open (§3) — this segment was already sitting at the ~2% baseline before seeding (confirmed by querying the actual data first), making it an unbiased pick, not one chosen to make the numbers look good after the fact.

**`denial_amount` literally equals `billing_amount`, including its negative sign, for `INVALID_BILLING_AMOUNT` denials.** This reads oddly (a "denied amount" that's negative), but it's the LLD's own stated Sprint 1 simplification (§1.2) — implemented literally rather than quietly adding an `abs()` the design didn't ask for.

**`denial_date` reuses `discharge_date`.** The source data has no real concept of "when a claim was denied," and no user story depends on that timing being meaningful — simplest honest choice over inventing a synthetic offset.

**Baseline denial rates (0.5% each for `RANDOM_AUDIT`/`HIGH_RISK_SCORE`) are tunable constants**, per the LLD explicitly leaving the exact background rate as an implementation detail, not a design decision.

**A gap between the LLD's narrative and its own formula, surfaced rather than silently patched over.** LLD §2 says a seeded negative-billing claim should score "close to 1." Under the literal formula (`0.6×denial_rate + 0.4×min(|z|/4,1)`), the real ceiling is `0.6×(worst segment's denial rate) + 0.4` ≈ 0.52 given this dataset's actual segment rates — nowhere near 1. The *ranking* story still holds (the negative-billing claims are provably the highest-scored claims in all 55,500 rows, now locked in by `test_negative_billing_claims_score_highest`), so the demo narrative works, but the absolute-score framing in the design doc doesn't match its own math. Flagged here rather than adjusting the weights myself — the LLD explicitly calls the 0.6/0.4 split untuned and not worth spending Sprint 1 time on, so retuning it wasn't this session's call to make unilaterally.

**Two real environment/compatibility bugs found and fixed, not just "ran the commands":**
1. `requirements.txt` declared `acryl-datahub` but not its `[sqlalchemy]` extra, and `sqlalchemy` wasn't installed at all — `datahub ingest` failed immediately. Fixed by installing the extra and updating `requirements.txt` to `acryl-datahub[sqlalchemy]==1.6.0.15` so a fresh clone doesn't hit the same wall.
2. Ingestion then failed with an `AvroTypeException` on `denial_model_scores`. Root-caused (via a minimal standalone repro, not guessing) to SQLite's inline `REFERENCES` syntax leaving foreign-key constraints unnamed after SQLAlchemy reflection — this DataHub version's Avro schema requires `ForeignKeyConstraintClass.name` to be non-null. Fixed by naming the FK constraints explicitly in `schema_sprint1.sql` (`CONSTRAINT fk_denials_claim_id FOREIGN KEY ...`). This had never surfaced before because none of the original 4 fixture tables have any foreign keys at all — Sprint 1's tables were the first to exercise this code path.

**`sqlite_sequence` got swept into DataHub as an 8th "table."** SQLite auto-creates this internal bookkeeping table because of the `AUTOINCREMENT` columns, and `ingest.yaml`'s `include_tables: true` has no exclusion filter. Harmless, but noted rather than silently left — not fixed here since it means touching `ingest.yaml`'s scope, which wasn't this session's decision to make alone.

## Not done in this session

- Sentinel/Investigator/Remediator/Scribe implementation (explicitly out of scope for Sprint 1 per the LLD).
- Any decision on filtering `sqlite_sequence` out of ingestion.
- Retuning the model's 0.6/0.4 weights (explicitly deferred, per above).
