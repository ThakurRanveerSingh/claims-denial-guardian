# Audit Report — INC-20260724T234736Z-cigna-obesity

**Status**: investigated
**Severity**: Critical
**Generated**: 2026-07-28T00:12:14.118173+00:00

## What was detected

Sentinel flagged Cigna / obesity for a denial rate of 16.0% (298 of 1864 claims), compared to a baseline of 3.8% built from every other segment (2046 of 53636 claims) — this segment is excluded from its own baseline, so a real spike here can't inflate the very baseline it's measured against. This is a statistically significant deviation: a standard two-proportion statistical test comparing this segment's rate to the baseline produces a z-score of 25.69, well above the flagging threshold of 3.5. The four counts behind this calculation — this segment's claims and denials, and the baseline's claims and denials — are 1864 and 298 (segment), 53636 and 2046 (baseline), stated here so the result can be independently recomputed. The exact calculation method is documented in the Technical Appendix below.

## What the investigation established

Root cause: a pre-existing issue inherited from raw_patients, the original source data. Confidence: high.

Sentinel flagged Cigna/obesity for a 15.99% denial rate (4.2x baseline). Breaking the 298 denials down by reason code shows 280 (93.96%) are INVALID_BILLING_AMOUNT, with a smaller unrelated remainder of 11 HIGH_RISK_SCORE and 7 RANDOM_AUDIT denials that have no field-level defect to trace by design. For the 280 INVALID_BILLING_AMOUNT claims, billing_amount < 0 was checked at every hop of the confirmed lineage chain (claims -> mart_billing -> staging_patients -> raw_patients), verifying row alignment via rowid at each hop before trusting the count. The negative value reproduces at exactly 280/280 (100%) at every single hop, including raw_patients, which has no further upstream lineage registered. This means the bad data was never introduced by any transform in this pipeline -- it is already present in the earliest available source data. The elevated denial rate for this segment is a downstream symptom of a pre-existing data-quality defect inherited from raw_patients, not a bug introduced by mart_billing, staging_patients, or the claims derivation itself.

The complete technical trace behind this conclusion — every query run and every check performed — is in the Technical Appendix below.

### Root cause breakdown

| Classification | Claims | % | Note |
|---|---|---|---|
| Inherited from raw_patients (original source data) | 280 | 94.0% | billing_amount < 0 reproduces at 100% across every hop (claims, mart_billing, staging_patients, raw_patients) for all 280 INVALID_BILLING_AMOUNT-denied claims in the segment. raw_patients has zero upstream lineage, so the negative value is already present in the earliest available data — not introduced by any transform in this pipeline. |
| High Risk Score (no data-quality hypothesis to trace) | 11 | 3.7% | Denials from the toy risk-scoring model exceeding a threshold. No underlying field-level defect to trace by design in this schema; not investigated further per scope. |
| Random Audit (no data-quality hypothesis to trace) | 7 | 2.4% | Baseline random-audit denials, unrelated to any data defect by design. Not investigated further per scope. |

## Member impact

Denial counts for this segment, by reason code — read live from the database at report-generation time, not from a cached figure:

| Denial reason | Claims |
|---|---|
| INVALID_BILLING_AMOUNT | 280 |
| HIGH_RISK_SCORE | 11 |
| RANDOM_AUDIT | 7 |

## Model health check

A feature-health check was run against the denial-risk scoring model (model version toy-denial-risk-v1). Overall result: all checks passed. This is a single-snapshot check of whether the model's input data still satisfies its own documented mathematical properties — not a comparison against historical data, since this project's dataset has no genuine earlier snapshot to compare against.

| Feature | Check | Result | Summary |
|---|---|---|---|
| segment_denial_rate | a data-integrity range check | Passed | segment_denial_rate stayed within its mathematically valid 0-100% range for every scored claim (observed range: 2.3% to 20.8%). This is a data-integrity check, not a distributional comparison — it always passes when the underlying calculation is working correctly, and would only fail if that calculation itself were broken. |
| billing_zscore | a check against the model's own documented boundary | Passed | 224 of 55500 scored claims (0.40%) have a billing_zscore beyond the model's own documented boundary of 4.0. Reported for visibility only — this version does not flag against any threshold here, to avoid treating an invented number as a meaningful cutoff. |
| billing_zscore | a shape comparison against the theoretical expected distribution | Passed | billing_zscore's observed shape has a Population Stability Index of 0.0384 against the theoretical standard normal distribution it's mathematically supposed to approximate — within the healthy range under the standard PSI convention (under 0.10 means no significant shift). This compares shape against a mathematical reference, not against a past snapshot of this data — no such historical snapshot genuinely exists for this project's dataset. |

## Actions taken

DataHub writeback (Scribe):
  raw_patients: tag already present, documentation note already present
  staging_patients: tag already present, documentation note already present
  mart_billing: tag already present, documentation note already present
  claims: tag already present, documentation note already present
  Documentation link: https://github.com/ThakurRanveerSingh/claims-denial-guardian/blob/main/examples/INC-20260724T234736Z-cigna-obesity/incident.json
Fix opened (Remediator):
  Pull request: https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/2
  File changed: transform/staging_patients.sql

## Outstanding items

Rows in staging_patients_quarantine (see the PR above for the exact count) require human review — suggested owner: clinical_team.

## Technical Appendix

This section is the raw technical trace Investigator used to reach its conclusion — included for reproducibility and engineering review. Compliance readers do not need to read this section.

**Statistical method**: `two_proportion_z_test()` in `src/agents/sentinel.py` (two-proportion z-test, leave-one-out baseline).

**Raw lineage trace**: get_lineage(claims, upstream, max_hops=3) -> mart_billing (degree 1), mart_demographics (degree 1), staging_patients (degree 2), raw_patients (degree 3) -> get_lineage_paths_between(raw_patients -> claims, direction=downstream) -> single path: raw_patients -> staging_patients -> mart_billing -> claims -> get_lineage(raw_patients, upstream, max_hops=3) -> 0 upstream entities (raw_patients is the pipeline root)

**Evidence log**:

| Step | Tool | Query/Call | Result |
|---|---|---|---|
| 1. Confirm schema of claims and denials | mcp__datahub__get_entities | get_entities([claims, denials]) | claims has source_billing_rowid, billing_amount, insurance_provider, medical_condition, etc. denials has claim_id (FK to claims), denial_reason_code, denial_amount. |
| 1b. Confirm schema of upstream tables before querying them | mcp__datahub__get_entities | get_entities([mart_billing, staging_patients, raw_patients]) | mart_billing: billing_amount REAL (no explicit FK column, sqlite implicit rowid). staging_patients: billing_amount TEXT, no FK column. raw_patients: billing_amount TEXT, no FK column, no schema pointer to further upstream. |
| 2. Walk lineage upstream from claims | mcp__datahub__get_lineage | get_lineage(urn=claims, upstream=true, max_hops=3) | 4 upstream datasets: mart_billing (degree 1), mart_demographics (degree 1), staging_patients (degree 2), raw_patients (degree 3). |
| 2b. Confirm exact path from raw_patients to claims | mcp__datahub__get_lineage_paths_between | get_lineage_paths_between(raw_patients, claims, direction=downstream) | Single dataset-level path: raw_patients -> staging_patients -> mart_billing -> claims. |
| 2c. Confirm raw_patients has no further upstream | mcp__datahub__get_lineage | get_lineage(urn=raw_patients, upstream=true, max_hops=3) | 0 upstream entities returned -- raw_patients is the root of the registered pipeline. |
| 3a. Confirm exact segment value strings | bash/sqlite3 | SELECT DISTINCT insurance_provider/medical_condition FROM claims WHERE ... LIKE '%igna%'/'%besi%' | Confirmed exact values: insurance_provider='Cigna', medical_condition='obesity'. |
| 3b. Break down flagged segment's denials by reason code | bash/sqlite3 | SELECT d.denial_reason_code, COUNT(*) FROM claims c JOIN denials d ON d.claim_id=c.claim_id WHERE insurance_provider='Cigna' AND medical_condition='obesity' GROUP BY d.denial_reason_code | INVALID_BILLING_AMOUNT=280, HIGH_RISK_SCORE=11, RANDOM_AUDIT=7 (sums to Sentinel's 298 denied). |
| 3c. Confirm INVALID_BILLING_AMOUNT claims actually have billing_amount<0 in claims | bash/sqlite3 | SELECT COUNT(*), SUM(CASE WHEN billing_amount<0 THEN 1 ELSE 0 END) FROM claims JOIN denials ... WHERE denial_reason_code='INVALID_BILLING_AMOUNT' | 280/280 have billing_amount < 0 in claims. |
| 4a. Test reproduction at immediate upstream hop (mart_billing) | bash/sqlite3 | LEFT JOIN mart_billing mb ON mb.rowid = c.source_billing_rowid; count mb.billing_amount<0 and missing joins | 280/280 negative in mart_billing, 0 missing joins -- reproduces 100% at first hop. |
| 4b. Verify mart_billing.rowid aligns with staging_patients.rowid before trusting next hop | bash/sqlite3 | JOIN staging_patients sp ON sp.rowid=mb.rowid; compare date_of_admission, discharge_date, numeric billing_amount | 55500/55500 rows align on dates and numeric billing_amount -- rowid join to staging_patients is valid (an earlier text-equality check on billing_amount undercounted due to TEXT vs REAL formatting, not misalignment). |
| 4c. Test reproduction at staging_patients | bash/sqlite3 | LEFT JOIN staging_patients sp ON sp.rowid = c.source_billing_rowid; count CAST(sp.billing_amount AS REAL)<0 | 280/280 negative in staging_patients, 0 missing joins -- still reproduces 100%. |
| 4d. Verify staging_patients.rowid aligns with raw_patients.rowid before trusting next hop | bash/sqlite3 | JOIN raw_patients rp ON rp.rowid=sp.rowid; compare dates and numeric billing_amount | 55500/55500 rows align -- rowid join to raw_patients is valid. |
| 4e. Test reproduction at raw_patients (final available hop) | bash/sqlite3 | LEFT JOIN raw_patients rp ON rp.rowid = c.source_billing_rowid; count CAST(rp.billing_amount AS REAL)<0 | 280/280 negative in raw_patients, 0 missing joins -- reproduces 100% all the way to the pipeline root. |

**Feature-health check** (drift-20260728T001207Z, 2026-07-28T00:12:07.313702+00:00): model_version toy-denial-risk-v1, written back to denial_risk_model in DataHub.

| Feature | Check type | Documented expected | Metric value | Status |
|---|---|---|---|---|
| segment_denial_rate | range_invariant | [0.0, 1.0] | 0.0000 | pass |
| billing_zscore | cap_exceedance | \|z\| <= 4.0 (BILLING_ZSCORE_CAP, score_claims.py) | 0.4036 | pass |
| billing_zscore | shape_vs_theoretical | PSI < 0.1 vs. theoretical standard normal (published PSI convention) | 0.0384 | pass |

---

Full incident record: https://github.com/ThakurRanveerSingh/claims-denial-guardian/blob/main/examples/INC-20260724T234736Z-cigna-obesity/incident.json
