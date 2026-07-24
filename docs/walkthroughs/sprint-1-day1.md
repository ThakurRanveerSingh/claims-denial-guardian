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

## UAT gap found: no MLModel entity in DataHub

UAT caught that Sprint 1 only ever registered `denial_model_scores` as a plain Dataset — no `MLModel`, `MLFeatureTable`, or `MLFeature` entity existed anywhere in DataHub. Confirmed empirically (`search(type: MLMODEL)` / `search(type: MLFEATURE_TABLE)` both returned `total: 0`) before writing any code, rather than assumed.

**This wasn't a missed requirement — it was a logged, deliberate trade-off** (LLD §4, last paragraph): *"No dedicated MLModel DataHub entity in Sprint 1... Flagged as the more-correct alternative to revisit if there's schedule slack."* The Production ML judging track is the new pressure that makes it worth spending that slack now.

**Two premise corrections made before writing any code:**
1. The task referenced `src/datahub/ingest_and_lineage.py` — that file never existed. Sprint 1 used the LLD's actual layout instead (`ingest.yaml` + extending `add_lineage.py`/`add_metadata.py` in place), an earlier agreed decision.
2. The task described the model as "our toy logistic-regression details." It isn't — `score_claims.py` is a deterministic weighted heuristic (LLD §2), explicitly *not* a trained model. Registering it in DataHub labeled "logistic regression" would have been false metadata, arguably worse than the missing entity. Registered as what it actually is instead, confirmed by the user before writing code.

### What was built

`src/datahub/register_ml_model.py` registers, against the live DataHub instance:
- 2 `MLFeature`s (`segment_denial_rate`, `billing_zscore`) under namespace `denial_risk_features`, each with a description and `sources` pointing back to `claims`. Feature *columns* (`insurance_provider`, `medical_condition`, `billing_amount`) are validated against claims' live schema (read via the graph, not hardcoded) before anything is emitted.
- 1 `MLFeatureTable` (`denial_risk_features`) grouping both features.
- 1 `MLModel` (`denial_risk_model`), with `properties` (accurate description, `mlFeatures`, `customProperties` incl. the model's actual weights), a `trainingData` aspect pointing at `claims`, and `ownership` (`claims_ops_team`, reusing the existing group).
- Platform for all three new entity types: a plain `python` `dataPlatform`, not `sqlite` (that's the *data* platform, not an ML one) and not something borrowed like `mlflow` (not actually in use here — would misrepresent the tooling).

`tests/test_ml_registration.py` (9 checks, requires live DataHub, skips rather than fails if unreachable) locks in: the 3 new entities exist, the model's description doesn't regress back to "logistic regression," ownership and feature linkage are correct, and `denial_model_scores`' original `claims` lineage edge (from Sprint 1) is still intact.

### Real problems hit and fixed while building this — not a clean run

1. **`MLFeature.sources` rejects `schemaField` (column-level) URNs.** First attempt pointed each feature's `sources` at the specific `claims` columns it's derived from (e.g. `insurance_provider`, `medical_condition`) via `schemaField` URNs — the live GMS rejected this with a 422: *"Entity type for urn ... is not a valid destination for field path: /sources/\*."* Confirmed via a standalone probe script before editing the real one. Fix: `sources` points at the `claims` dataset as a whole; the specific columns are named in each feature's description text instead, since that's the only place this granularity can go.

2. **No client-facing mechanism makes `MLModel` a graph-traversable upstream of a `Dataset`, in this DataHub version.** Tried two different approaches, both explicitly rejected by the live server:
   - `UpstreamLineageClass` / `UpstreamClass.dataset` — strictly typed server-side as `DatasetUrn`; passing the model's URN threw a 422 ("Unable to instantiate urn type: DatasetUrn").
   - The `updateLineage` GraphQL mutation (DataHub's manual/UI lineage mechanism, which takes plain `String` urns with no apparent type constraint in its schema) — still rejected server-side: *"Tried to add lineage edge with non-dataset node when we expect a dataset."*
   - Checked whether a `DataJob` detour would help (DataHub's standard way to represent a process with inputs/outputs) — it wouldn't: `DataJobInputOutputClass.inputDatasets`/`outputDatasets` only accept Dataset URNs too, so the model still wouldn't be a literal node in that path.
   - **Resolution, confirmed with the user**: the `denial_risk_model → denial_model_scores` hop is documented, not graph-traversable — a `produced_by_model` custom property on `denial_model_scores` (added via `DatasetPatchBuilder`, which patches in the one key without touching the dataset's existing properties), matched by an `output_dataset` custom property on the model itself. 3 of the 4 requested hops (`claims → features → feature table → model`) are real, clickable lineage; this 4th one is a documented cross-reference only.

3. **A `trainingData` GraphQL query returned `None` even though the aspect had been emitted successfully.** Checked the raw aspect directly (`graph.get_aspect` / `graph.get_entity_raw`) rather than trusting the GraphQL resolver, and confirmed `mlModelTrainingData` was correctly persisted server-side — a resolver quirk in this GMS version (similar in kind to the `denials` search-index lag from Sprint 1), not a real data problem.

### How to verify in the DataHub UI

1. Open the DataHub UI at **http://localhost:9002** (GMS/API is :8080, UI is :9002).
2. Search **`denial_risk_model`** in the top search bar. It should appear under the **ML Models** entity type. Open it.
   - **Documentation/Properties tab**: description should read "Toy denial-risk scorer... a deterministic, explicitly untuned weighted-sum heuristic" — NOT "logistic regression." Custom properties should list `model_version`, `denial_rate_weight` (0.6), `billing_zscore_weight` (0.4), `billing_zscore_cap` (4.0), and `output_dataset` (pointing at `denial_model_scores`).
   - **Ownership tab**: should show `claims_ops_team`.
   - **Lineage tab**: should show `claims` upstream (via training data) and the two features/feature table connected.
3. Search **`denial_risk_features`** — the MLFeatureTable. Open it; its **Features** tab should list `segment_denial_rate` and `billing_zscore`.
4. Search **`denial_model_scores`** (the existing dataset from Sprint 1). Open it.
   - **Lineage tab**: should still show `claims` upstream, same as before — this run shouldn't have changed that.
   - **Properties tab**: should show a `produced_by_model` custom property pointing at `denial_risk_model`. This is intentionally *not* an arrow in the Lineage tab — see the resolution above for why.
