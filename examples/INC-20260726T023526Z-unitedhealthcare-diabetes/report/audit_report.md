# Audit Report — INC-20260726T023526Z-unitedhealthcare-diabetes

**Status**: investigated
**Severity**: Critical
**Generated**: 2026-07-28T00:11:56.900061+00:00

## What was detected

Sentinel flagged UnitedHealthcare / diabetes for a denial rate of 20.8% (375 of 1806 claims), compared to a baseline of 3.7% built from every other segment (1969 of 53694 claims) — this segment is excluded from its own baseline, so a real spike here can't inflate the very baseline it's measured against. This is a statistically significant deviation: a standard two-proportion statistical test comparing this segment's rate to the baseline produces a z-score of 35.53, well above the flagging threshold of 3.5. The four counts behind this calculation — this segment's claims and denials, and the baseline's claims and denials — are 1806 and 375 (segment), 53694 and 1969 (baseline), stated here so the result can be independently recomputed. The exact calculation method is documented in the Technical Appendix below.

## What the investigation established

Root cause: a defect introduced during the claims build process itself. Confidence: high.

Sentinel flagged UnitedHealthcare/diabetes for a denial rate 5.7x baseline. Breaking the 375 denials down by reason code, 361 (96.3%) are INVALID_BILLING_AMOUNT -- the only reason code with a testable data-quality hypothesis (billing_amount < 0) in this schema; RANDOM_AUDIT (8) and HIGH_RISK_SCORE (6) have no field-level defect to trace by design. Of the 361, walking the lineage one hop at a time (claims -> mart_billing -> staging_patients -> raw_patients) shows two distinct, non-overlapping causes. The dominant one (325 claims, 86.7% of all flagged denials) is a sign-flip bug: mart_billing holds the correct positive billing_amount, but claims holds its exact negation -- the defect is introduced in the process that builds claims from mart_billing, not upstream. A whole-table check confirms this exact sign-flip pattern occurs nowhere else in claims outside this segment, which is the direct explanation for the anomalous spike. A separate, smaller group (36 claims, 9.6%) already has negative billing_amount at every upstream hop including raw_patients, the root of the lineage with no further upstream -- a pre-existing source-data defect that was simply passed through unchanged (mart_billing and staging_patients were verified as exact, zero-mismatch passthroughs across their entire tables, not just this segment). Remediation should target (a) the claims-build logic for the sign-flip, and (b) raw_patients data entry/collection for the smaller inherited defect.

Also checked and confirmed clean, with no defect found: mart_billing, staging_patients.

The complete technical trace behind this conclusion — every query run and every check performed — is in the Technical Appendix below.

### Root cause breakdown

| Classification | Claims | % | Note |
|---|---|---|---|
| Sign-flip bug in claims build from mart_billing | 325 | 86.7% | mart_billing.billing_amount is positive and exactly equals -1 * claims.billing_amount for these rows -- the value is correct one hop upstream and negated by the time it lands in claims. A full-claims-table check (not restricted to this segment) found every instance of this exact sign-flip pattern belongs to UnitedHealthcare/diabetes -- 0 occurrences elsewhere -- which is why this specific segment's denial rate spiked. |
| Pre-existing negative billing_amount in source data | 36 | 9.6% | billing_amount is negative and value-identical across mart_billing, staging_patients, and raw_patients (0 mismatches at each hop, checked table-wide). raw_patients has no further upstream lineage, so this is a pre-existing source-data defect, not something introduced by any pipeline transformation. |
| Random Audit (no data-quality hypothesis to trace) | 8 | 2.1% | Random audit denials have no underlying field-level defect to trace by design; not investigated further. |
| High Risk Score (no data-quality hypothesis to trace) | 6 | 1.6% | Model risk-score denials have no underlying field-level defect to trace by design; not investigated further. |

## Member impact

Denial counts for this segment, by reason code — read live from the database at report-generation time, not from a cached figure:

| Denial reason | Claims |
|---|---|
| INVALID_BILLING_AMOUNT | 361 |
| RANDOM_AUDIT | 8 |
| HIGH_RISK_SCORE | 6 |

## Model health check

A feature-health check was run against the denial-risk scoring model (model version toy-denial-risk-v1). Overall result: all checks passed. This is a single-snapshot check of whether the model's input data still satisfies its own documented mathematical properties — not a comparison against historical data, since this project's dataset has no genuine earlier snapshot to compare against.

| Feature | Check | Result | Summary |
|---|---|---|---|
| segment_denial_rate | a data-integrity range check | Passed | segment_denial_rate stayed within its mathematically valid 0-100% range for every scored claim (observed range: 2.3% to 20.8%). This is a data-integrity check, not a distributional comparison — it always passes when the underlying calculation is working correctly, and would only fail if that calculation itself were broken. |
| billing_zscore | a check against the model's own documented boundary | Passed | 224 of 55500 scored claims (0.40%) have a billing_zscore beyond the model's own documented boundary of 4.0. Reported for visibility only — this version does not flag against any threshold here, to avoid treating an invented number as a meaningful cutoff. |
| billing_zscore | a shape comparison against the theoretical expected distribution | Passed | billing_zscore's observed shape has a Population Stability Index of 0.0384 against the theoretical standard normal distribution it's mathematically supposed to approximate — within the healthy range under the standard PSI convention (under 0.10 means no significant shift). This compares shape against a mathematical reference, not against a past snapshot of this data — no such historical snapshot genuinely exists for this project's dataset. |

## Actions taken

DataHub writeback (Scribe):
  claims: tag already present, documentation note already present
  raw_patients: tag already present, documentation note already present
  Documentation link: https://github.com/ThakurRanveerSingh/claims-denial-guardian/blob/main/examples/INC-20260726T023526Z-unitedhealthcare-diabetes/incident.json
Fix opened (Remediator):
  Pull request: https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/1
  File changed: transform/claims.sql

## Outstanding items

Rows in claims_quarantine (see the PR above for the exact count) require human review — suggested owner: claims_ops_team.

## Technical Appendix

This section is the raw technical trace Investigator used to reach its conclusion — included for reproducibility and engineering review. Compliance readers do not need to read this section.

**Statistical method**: `two_proportion_z_test()` in `src/agents/sentinel.py` (two-proportion z-test, leave-one-out baseline).

**Raw lineage trace**: get_lineage(upstream, urn=claims, max_hops=3) -> mart_billing (degree 1), mart_demographics (degree 1), staging_patients (degree 2), raw_patients (degree 3) -> get_lineage(upstream, urn=raw_patients, max_hops=2) -> empty; raw_patients is the root of the chain, no further upstream

**Evidence log**:

| Step | Tool | Query/Call | Result |
|---|---|---|---|
| 1. Confirm live schema of claims and denials | Bash (sqlite3 .schema) | .schema claims / .schema denials / .tables | claims has source_billing_rowid (FK-by-convention to mart_billing.rowid) and billing_amount REAL NOT NULL. denials has denial_reason_code with documented values INVALID_BILLING_AMOUNT \| HIGH_RISK_SCORE \| RANDOM_AUDIT. Full table list also surfaced mart_billing, mart_demographics, staging_patients, raw_patients, denial_model_scores, and passthrough views. |
| 1b. Confirm live schema of upstream tables | Bash (sqlite3 .schema) | .schema mart_billing / mart_demographics / staging_patients / raw_patients / denial_model_scores / views | mart_billing and staging_patients carry a pipeline_status column; v_staging_from_raw is a straight passthrough of raw_patients with lower/trim cleaning columns added; v_billing_from_staging casts staging billing_amount to REAL with no filtering or sign correction. |
| 2. Walk lineage upstream from claims, live | mcp__datahub__get_lineage | get_lineage(urn=claims, upstream=true, max_hops=3) | 4 upstream datasets returned: mart_billing (degree 1, tagged critical, owned by finance_team), mart_demographics (degree 1, owned by research_team), staging_patients (degree 2), raw_patients (degree 3, tagged pii + quality_monitored). Confirms chain claims -> mart_billing -> staging_patients -> raw_patients, plus a separate claims -> mart_demographics branch unrelated to billing_amount. |
| 3. Break flagged segment's denials down by denial_reason_code | Bash (sqlite3) | SELECT denial_reason_code, COUNT(*) FROM claims JOIN denials ... WHERE insurance_provider='UnitedHealthcare' AND medical_condition='diabetes' GROUP BY denial_reason_code | INVALID_BILLING_AMOUNT=361, RANDOM_AUDIT=8, HIGH_RISK_SCORE=6 (sums to 375, matching Sentinel's flagged count). Only INVALID_BILLING_AMOUNT maps to a testable hypothesis (billing_amount < 0) per the schema's documented semantics. |
| 4a. Confirm anomaly at claims itself | Bash (sqlite3) | SELECT SUM(CASE WHEN billing_amount<0...), COUNT(*) FROM claims JOIN denials WHERE ... AND denial_reason_code='INVALID_BILLING_AMOUNT' | 361/361 (100%) have billing_amount < 0 in claims, confirming the reason code is consistent with the documented negative-billing-amount defect. |
| 4b. Test reproduction at immediate upstream hop (mart_billing) | Bash (sqlite3) | LEFT JOIN mart_billing mb ON c.source_billing_rowid = mb.rowid; count mb.billing_amount < 0 and missing joins | 0 missing joins (join key is sound). Only 36/361 (10%) reproduce (mb.billing_amount < 0); 325/361 (90%) do NOT reproduce -- mart_billing shows a positive value where claims shows negative. Anomaly does not reproduce for near-all rows at hop 1, but a non-trivial 36-row remainder does -- per the protocol this requires walking further before concluding 'introduced here'. |
| 4c. Characterize the 325 non-reproducing rows | Bash (sqlite3) | SELECT SUM(CASE WHEN mb.billing_amount = -1*c.billing_amount ...) vs total non-reproducing; sample rows | All 325/325 non-reproducing rows satisfy mart_billing.billing_amount = -1 * claims.billing_amount exactly -- a precise sign flip, not random corruption. Sampled rows confirm (e.g. CLM-000069: mart=+35776.82, claims=-35776.82). |
| 4d. Test reproduction of the remaining 36 at next hop (staging_patients) | Bash (sqlite3) | JOIN mart_billing.rowid = staging_patients.rowid (full-table mismatch check) + segment-filtered negative count | 0 mismatches between mart_billing.billing_amount and CAST(staging_patients.billing_amount) across the ENTIRE table (exact passthrough, confirms rowid alignment is a true 1:1 mapping, not coincidental). All 36/36 flagged rows reproduce billing_amount < 0 at staging_patients. |
| 4e. Test reproduction of the remaining 36 at next hop (raw_patients) | Bash (sqlite3) | JOIN staging_patients.rowid = raw_patients.rowid (full-table mismatch check) + segment-filtered negative count | 0 mismatches between staging_patients.billing_amount and raw_patients.billing_amount across the entire table (exact passthrough, consistent with v_staging_from_raw being a pure passthrough view). All 36/36 flagged rows reproduce billing_amount < 0 at raw_patients. |
| 4f. Confirm raw_patients is the root (no further upstream) | mcp__datahub__get_lineage | get_lineage(urn=raw_patients, upstream=true, max_hops=2) | 0 upstream entities returned -- raw_patients is the root source table, so the 36-row defect is classified inherited_from:raw_patients rather than needing further tracing. |
| 5. Quantify each explanation as a fraction of all 375 flagged denials | Bash (sqlite3, arithmetic) | 325/375, 36/375, 8/375, 6/375 | 86.67% sign-flip introduced at claims, 9.6% inherited pre-existing defect from raw_patients, 2.13% RANDOM_AUDIT (no hypothesis), 1.6% HIGH_RISK_SCORE (no hypothesis). Reported as four separate root_cause_breakdown entries rather than blended. |
| 6. Check whether the sign-flip bug is segment-specific or systemic | Bash (sqlite3) | SELECT insurance_provider, medical_condition, COUNT(*) FROM claims JOIN mart_billing WHERE mb.billing_amount = -1*c.billing_amount AND mb.billing_amount>=0 GROUP BY 1,2 (whole-table, not segment-filtered) | All 325 sign-flip occurrences in the entire claims table belong to UnitedHealthcare/diabetes -- 0 elsewhere. The claims-build bug is fully localized to this exact segment, directly explaining why Sentinel's z-score flagged only this segment. |

**Feature-health check** (drift-20260728T001150Z, 2026-07-28T00:11:50.734377+00:00): model_version toy-denial-risk-v1, written back to denial_risk_model in DataHub.

| Feature | Check type | Documented expected | Metric value | Status |
|---|---|---|---|---|
| segment_denial_rate | range_invariant | [0.0, 1.0] | 0.0000 | pass |
| billing_zscore | cap_exceedance | \|z\| <= 4.0 (BILLING_ZSCORE_CAP, score_claims.py) | 0.4036 | pass |
| billing_zscore | shape_vs_theoretical | PSI < 0.1 vs. theoretical standard normal (published PSI convention) | 0.0384 | pass |

---

Full incident record: https://github.com/ThakurRanveerSingh/claims-denial-guardian/blob/main/examples/INC-20260726T023526Z-unitedhealthcare-diabetes/incident.json
