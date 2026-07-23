# Low-Level Design — Sprint 1

Status: Draft — ready for review before implementation
Scope: claims/denials schema extension, the toy denial-scoring model, and their ingestion + lineage registration into DataHub. **No implementation code in this document — design only.**
Out of scope for Sprint 1: Sentinel/Investigator/Remediator/Scribe implementation, the actual drift-check logic (US-6, P2), GitHub PR flow, audit report generation.

## 0. Grounding — what's actually there

Before designing anything I inspected the real environment instead of assuming. This changed several decisions below, so it's worth recording what I found and why it mattered.

**`healthcare.db` exists, but not in this repo.** It lives at `~/static-assets/datasets/healthcare/healthcare.db`, alongside `create_db.py`, `ingest.yaml`, `add_lineage.py`, `add_metadata.py`, and a README. This is a general-purpose, reusable teaching fixture (Kaggle "Healthcare Dataset", CC0, `random.seed(42)` for reproducibility) — not something specific to this project, and not something we should mutate in place, since Sprint 1 needs to add tables and additional seeded data on top of it. **Decision: copy the whole `healthcare/` folder's contents into `src/datahub/` in this repo** and do all Sprint 1 work against the copy. Rationale: keeps the demo fully self-contained and reproducible for anyone who clones the repo, and never risks corrupting a shared fixture other exercises might use pristine. Rejected alternative: work against the file in place at `~/static-assets/...` — rejected because a hackathon submission that depends on a file outside the repo isn't reproducible by judges.

**The real schema differs from what a clean-room design would assume.** Actual tables (verified via `sqlite3 healthcare.db .schema`):

```
raw_patients        (all TEXT — name, age, gender, blood_type, medical_condition,
                      date_of_admission, doctor, hospital, insurance_provider,
                      billing_amount, room_number, admission_type, discharge_date,
                      medication, test_results)
staging_patients     (raw_patients columns + *_clean variants + pipeline_status)
mart_billing         (name, hospital, insurance_provider, admission_type,
                      billing_amount REAL, date_of_admission, discharge_date,
                      length_of_stay_days, medication, pipeline_status)
mart_demographics    (name, age INT, gender, blood_type, medical_condition,
                      hospital, test_results, pipeline_status)
```

There is **no patient ID and no claim ID anywhere** — `name` is the only identity signal, and it is not unique (49,552 distinct names across 55,500 rows). This directly shapes the `claims` table design in §1.

**Data quality issues are already planted — including the one US-2 names.** `create_db.py` plants four issues into `raw_patients`, seeded and reproducible:

| Issue | Rate | Rows (actual) |
|---|---|---|
| Negative `billing_amount` | ~2% | 1,215 confirmed via query |
| NULL `name` | ~1% | ~555 |
| Invalid `age` (< 0 or > 120) | ~1.5% | ~830 |
| `date_of_admission` / `discharge_date` swapped | ~0.5% | ~275 |

US-2's acceptance criterion says "the planted issue (e.g., negative billing)" — **this issue already exists**; Sprint 1 does not need to invent a data-quality bug from scratch. What it *does* need to do is turn that into a segment-level anomaly (see §2), because as generated, the negative-billing rows are picked uniformly at random across the whole table — there's no single segment where the rate spikes yet.

**DataHub is running, but only half-registered.** Confirmed via GraphQL search against `localhost:8080`: `raw_patients`, `staging_patients`, `mart_billing`, `mart_demographics` (and their lineage views) exist as Dataset entities with URNs of the form `urn:li:dataset:(urn:li:dataPlatform:sqlite,healthcare.main.<table>,PROD)` — meaning `datahub ingest -c ingest.yaml` has been run. But querying `mart_billing`'s `upstreamLineage` aspect returns nothing — `add_lineage.py` and `add_metadata.py` have **not** been run. Without them, there is no lineage graph at all yet, and Investigator's trace (US-2) would have nothing to walk even before claims/denials exist. **This is now an explicit Sprint 1 prerequisite** (§3), not an assumption.

**`mart_billing` and `mart_demographics` are row-aligned by `rowid`.** Both are created via plain `CREATE TABLE ... AS SELECT ... FROM staging_patients` with no filtering, join, or aggregation — so row N in one corresponds to row N in the other. Verified empirically: joining on `rowid` matches `hospital` for 55,500/55,500 rows and `name` for 54,945/55,500 (the gap is exactly the ~555 NULL-name rows, where SQL's `NULL = NULL` correctly evaluates to unknown rather than true — not a real misalignment). **This matters because `mart_billing` alone has no `medical_condition` column**, and `medical_condition` is one of the three segment dimensions US-1 needs. `rowid` join is the reliable way to pull it in from `mart_demographics` — far better than joining on `name`+`hospital`, which I checked separately and found collides on ~10.7% of rows.

## 1. Schema extension

### 1.1 `claims`

```sql
CREATE TABLE claims (
    claim_id              TEXT PRIMARY KEY,   -- 'CLM-' || printf('%06d', source_billing_rowid)
    source_billing_rowid  INTEGER NOT NULL,   -- mart_billing.rowid this claim was derived from
    patient_name           TEXT,               -- mart_billing.name — see note below
    hospital                TEXT NOT NULL,
    insurance_provider       TEXT NOT NULL,
    medical_condition          TEXT,             -- joined from mart_demographics via rowid
    admission_type               TEXT,
    billing_amount                 REAL NOT NULL,
    date_of_admission                TEXT,
    discharge_date                     TEXT,
    length_of_stay_days                  REAL,
    medication                             TEXT
);
```

**Why one claim per admission record:** the source dataset has no separate concept of "claim" — it's admission/billing records. Treating each `mart_billing` row as exactly one claim is the simplest honest mapping and matches how the six user stories talk about claims (one billed event, possibly denied).

**Why `claim_id` is derived from `mart_billing.rowid`, not a random UUID:** it's deterministic, traceable straight back to a specific source row (useful for debugging and for Investigator's narrative — "claim CLM-014293 came from mart_billing row 14293"), and reproducible across re-runs given the fixture's fixed seed. A UUID would work but adds nothing and loses that traceability.

**Why `patient_name` is included despite not being a real identifier:** US-5's audit report needs to say something about "which members' claims were affected." `name` is the only signal available. It is explicitly *not* a patient ID — flagged here so nothing downstream treats it as one. A production system would need a real member ID upstream; that's out of scope for a hackathon built on this fixture.

**Why `medical_condition` is joined in from `mart_demographics` (via `rowid`, not `name`/`hospital`):** US-1 needs condition as a segment dimension, and it doesn't exist in `mart_billing`. This does mean `claims`' true upstream is *two* tables, not one — see the lineage note in §3.

**Rejected: denormalizing segment keys onto `denials` for query speed.** At 55,500 rows, a `JOIN` from `denials` back to `claims` for `insurance_provider`/`medical_condition` costs nothing in SQLite. Denormalizing would just add a second place those values could drift out of sync, for zero real benefit at this scale.

### 1.2 `denials`

```sql
CREATE TABLE denials (
    denial_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id               TEXT NOT NULL REFERENCES claims(claim_id),
    denial_date               TEXT NOT NULL,
    denial_reason_code          TEXT NOT NULL,  -- see enumeration below
    denial_amount                  REAL NOT NULL  -- Sprint 1: always = claims.billing_amount
);
```

**Why a separate table instead of a `status` column on `claims`:** a claim can in principle be denied, appealed, and denied again — modeling denial as an *event* rather than a mutable status is both more realistic and lets Sentinel count denials per pipeline run rather than reading a single overwritten field. Also matches the diagram's separate "denials" box.

**Why `denial_id` is a plain autoincrement, unlike `claim_id`:** there's no external source row for a denial to trace back to — it's synthetic data Sprint 1 generates — so there's nothing to derive a meaningful ID from. A UUID/rowid distinction that mattered for `claims` doesn't apply here.

**Reason code enumeration** (small, closed set — needed so Investigator has something specific to point to):

| Code | Meaning | Drives the demo? |
|---|---|---|
| `INVALID_BILLING_AMOUNT` | `billing_amount < 0` on the source claim | **Yes — this is the seeded anomaly (§2)** |
| `HIGH_RISK_SCORE` | Toy model's `risk_score` above threshold | No — background realism |
| `RANDOM_AUDIT` | Small baseline rate of unrelated denials | No — background realism, keeps the dataset from looking artificially clean |

**Sprint 1 simplification, stated explicitly:** `denial_amount` always equals `claims.billing_amount` (full denial only). Real payers do partial denials; none of the six user stories require modeling that, so it's left out rather than built speculatively.

### 1.3 `denial_model_scores`

```sql
CREATE TABLE denial_model_scores (
    score_id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id                        TEXT NOT NULL REFERENCES claims(claim_id),
    model_version                     TEXT NOT NULL,   -- 'toy-denial-risk-v1'
    risk_score                          REAL NOT NULL,   -- 0..1
    segment_denial_rate_feature           REAL,           -- raw feature value, stored for future drift checks
    billing_zscore_feature                  REAL,           -- raw feature value, stored for future drift checks
    scored_at                                 TEXT NOT NULL
);
```

**Why scores live in their own table, not a column on `claims` or `denials`:** the model scores *every* claim (predicting denial risk), independent of whether that claim actually ended up denied — scoring only denied claims would defeat the point of a risk model. Keeping it separate also allows re-scoring with a new `model_version` later without mutating `claims`.

**Why the raw feature values are stored, not just the final score:** US-6 (drift monitoring) is P2 and explicitly deferred past Sprint 1, but the *substrate* for it — being able to compare a feature's distribution today against a stored baseline — needs to exist before that logic can be written. Storing `risk_score` alone would mean Sprint 2 has to recompute features from scratch to check drift. This is the one place Sprint 1 deliberately builds a little ahead of the immediate requirement, because the alternative is redoing this same schema work later under time pressure.

## 2. The toy denial-scoring model

**Design choice: a deterministic weighted heuristic, not a trained ML model.** Given the "toy" framing in the requirements and the Aug 3 cut-line pressure, a trained model (e.g. scikit-learn) would add a new dependency, a training script, model serialization, and non-determinism in what's supposed to be a fully explainable demo — for no requirement that actually asks for predictive accuracy. A heuristic is instant to compute, trivially reproducible, and just as capable of feeding a drift signal (US-6 only needs *a* signal that can shift, not a validated model).

**Segment definition: `(insurance_provider, medical_condition)`, not the full 3-way `(provider, condition, hospital)` from US-1's literal text.** `hospital` has 39,876 distinct values across 55,500 rows — most hospitals appear once or twice. A 3-way segment including hospital would mostly be buckets of size 1, with no meaningful baseline to detect a "spike" against. `(provider, condition)` gives 5 × 6 = 30 segments at ~1,850 rows each — enough volume for a rate to be statistically meaningful. This is recorded as a cross-cutting risk in the HLD (§4) since it affects Sentinel's future design too, not just Sprint 1.

**Formula:**

```
segment_denial_rate  = denied claims in (provider, condition) / total claims in (provider, condition)
billing_zscore        = (claim.billing_amount − segment_mean_billing) / segment_stddev_billing
risk_score             = clamp01( 0.6 × segment_denial_rate + 0.4 × min(|billing_zscore| / 4, 1.0) )
```

**Why these two features:** `segment_denial_rate` is the strongest realistic signal a denial-risk model would use. `billing_zscore` ties the score directly to the demo's actual root-cause story — a claim with the seeded negative billing amount will have a huge z-score and a score close to 1, so Investigator's narrative ("this claim was flagged as high-risk *and* it's the one with the anomalous billing amount") holds together end to end.

**Why the weights (0.6/0.4) are not tuned:** they're an untuned placeholder split, stated as such. This is a heuristic, not a fitted model — pretending otherwise would misrepresent what it is. Easy to adjust later; not worth spending Sprint 1 time tuning weights nothing depends on being "correct."

**The circularity this accepts, stated plainly:** `segment_denial_rate` is computed *from* the denials table this same sprint generates, then used to score claims in that same dataset — this is a backfill/demo scorer, not a model trained on history to score incoming claims. A production version would train on historical denials and score new claims. Calling this out so it's not mistaken for more than it is.

## 3. Denial generation (the seeded anomaly)

Sprint 1 needs a concrete rule for which claims get a `denials` row, because `denials` doesn't exist as a concept in the source fixture at all — this is new synthetic data, not a re-shaping of something already there.

**Rule:**
1. Every claim with `billing_amount < 0` gets denied, reason `INVALID_BILLING_AMOUNT`. This is a completely realistic rule on its own (payers don't pay negative charges) and reuses the fixture's existing, already-tagged, already-glossary'd "Billing Amount... must always be positive" issue rather than inventing a new defect type.
2. A small baseline of claims get denied for `RANDOM_AUDIT` / `HIGH_RISK_SCORE` reasons, so the dataset doesn't look artificially clean (exact background rate is an implementation-time parameter, not an architectural decision).
3. **The spike, specifically:** because rule 1's negative-billing rows are spread uniformly at random across all 30 segments (per `create_db.py`'s plain `ORDER BY RANDOM()` selection), the resulting denial rate is roughly flat across segments — not a demo-worthy spike on its own. Sprint 1 needs one additional, explicit step: pick **one** `(provider, condition)` segment and increase its negative-billing concentration well above the ~2% baseline (e.g. by converting a larger share of that segment's positive `billing_amount` values to negative, using the same reproducible-seed approach `create_db.py` already uses). That segment is what Sentinel should flag and what Investigator should trace.

**Open parameter, not an architecture decision:** exactly which `(provider, condition)` pair gets the seeded spike, and how large the effect size is, is a data/demo-storytelling choice, not a structural one — it can be picked at implementation time (e.g. `UnitedHealthcare` × `Diabetes` as one reasonable option) without revisiting this design.

## 4. Ingestion + lineage registration into DataHub

This extends the existing fixture's own scripts rather than inventing a parallel mechanism, since those scripts already establish the exact URN scheme, tag/glossary/ownership taxonomy, and emitter pattern this DataHub instance uses.

**Step 0 (prerequisite, not new work): run the fixture's own scripts first.**
```
datahub ingest -c ingest.yaml     # already run once — re-run after copying into repo
python add_lineage.py             # NOT yet run — establishes raw → staging → mart_billing/mart_demographics
python add_metadata.py            # NOT yet run — tags, glossary, ownership on the 4 existing tables
```
Without this, the graph Investigator needs to walk doesn't exist even after claims/denials are added (HLD §4, risk 1).

**Step 1 — schema registration for the 3 new tables: no new code needed.** `ingest.yaml`'s source is `type: sqlalchemy`, `include_tables: true` — it auto-introspects every table in the SQLite file. Once `claims`, `denials`, and `denial_model_scores` exist in the copied `healthcare.db`, re-running `datahub ingest -c ingest.yaml` registers their schemas automatically, using the same `platform_instance: healthcare` so it extends the existing entities rather than creating a parallel, disconnected set.

**Step 2 — lineage: extend `add_lineage.py`'s dictionaries, don't write a new script.** The existing script's `TABLE_LINEAGE` dict is exactly the right shape for this — it maps a downstream table to its upstream table name(s), then emits `UpstreamLineageClass` via `MetadataChangeProposalWrapper` / `DatahubRestEmitter` (already-proven pattern, same emitter the project's `CLAUDE.md` write-path convention expects). Sprint 1 adds three entries:

```python
TABLE_LINEAGE = {
    # ...existing entries...
    "claims":              ["mart_billing", "mart_demographics"],  # see §1.1 — two real upstreams
    "denials":              ["claims"],
    "denial_model_scores":    ["claims"],
}
```

**Step 3 — metadata: extend `add_metadata.py`'s dictionaries.** Reuses the existing taxonomy rather than inventing a new one:
- Tags: add `claims`, `denials`, `denial_model_scores` to `pipeline_stage` (they're new pipeline stages); add `denials` to `critical` (direct financial impact, same reasoning as `mart_billing`); add `claims`, `denials` to `quality_monitored` (Sentinel actively checks them).
- Glossary: two new terms — "Denial Reason Code" (defines the three-code enumeration from §1.2) attached to `denials`; "Denial Risk Score" (defines the heuristic and its 0-1 range, explicitly noting it's untuned) attached to `denial_model_scores`.
- Ownership: a new group, `claims_ops_team`, owning `claims`/`denials`/`denial_model_scores` — distinct from the existing `finance_team`/`clinical_team`/`research_team`, and named to match US-1's "claims operations lead" persona directly.

**No dedicated `MLModel` DataHub entity in Sprint 1.** DataHub supports a proper `MLModel`/`MLFeatureTable` entity type for this, which would be the more "correct" way to model a model's lineage. But nothing in this DataHub instance uses that entity type yet — everything so far is plain Dataset + Tag + GlossaryTerm. Introducing a new entity type for one table, on a hackathon timeline, is more DataHub-API surface than the requirement needs: US-6 only asks for "the model's lineage visible in DataHub," which dataset-level lineage into `denial_model_scores` plus the glossary term already satisfies. Flagged as the more-correct alternative to revisit if there's schedule slack (ties to the P2 "simplified version" framing already in the negotiated scope).

## 5. Where things live

```
src/datahub/
  healthcare.db          # copied from ~/static-assets/datasets/healthcare/, extended in place
  ingest.yaml             # copied as-is
  create_db.py             # copied as-is, kept for provenance/regeneration reference
  add_lineage.py             # copied, extended per §4 step 2
  add_metadata.py              # copied, extended per §4 step 3
  schema_sprint1.sql             # NEW — claims/denials/denial_model_scores DDL from §1
  generate_denials.py              # NEW — implements §3's denial rule + seeded spike (not written yet)
  score_claims.py                    # NEW — implements §2's scoring formula (not written yet)
```

**Why copy the whole fixture folder rather than just the `.db` file:** `add_lineage.py`/`add_metadata.py` are tightly coupled to this exact database and URN scheme (hardcoded `PLATFORM = "sqlite"`, `DEFAULT_INSTANCE = "healthcare"`). Keeping them together with the data they describe — and extending them in place — avoids maintaining two lineage scripts that quietly drift apart.

**Why `src/datahub/` and not a top-level `data/` folder:** matches where the Sprint 0 scaffold already designated DataHub-related code to live, and mirrors the upstream fixture's own flat layout (db file sits next to the scripts that manage it) rather than introducing a new top-level folder.

## 6. What Sprint 1 does *not* decide

- The exact `(provider, condition)` pair for the seeded spike, and its effect size (§3 — implementation-time parameter).
- Sentinel's actual anomaly-detection algorithm (threshold, statistical test) — HLD §4 risk 2 gives a strong recommendation on segmentation, but the detection logic itself is Sentinel's implementation, not this LLD.
- Any retry/escalation behavior if the seeded spike turns out not to be detectable as designed — that's validated when Sentinel is actually built and run against this data, not decided in advance.
