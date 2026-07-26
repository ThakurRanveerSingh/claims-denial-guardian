## Guardian-generated fix

**Incident**: `INC-20260724T234736Z-cigna-obesity`
**Segment**: Cigna / obesity (z = 25.69)
**Root cause**: `inherited_from:raw_patients`
**Confidence**: high

## What changed

`transform/staging_patients.sql` now splits rows at build time: clean rows
(`billing_amount >= 0`) continue to `staging_patients` as
before; violating rows are routed to a new
`staging_patients_quarantine` table instead of silently landing in
`staging_patients`.

## Why quarantine, not correction

The tempting fix here would be to correct the values directly (e.g.
`ABS(billing_amount)`) and make the anomaly disappear. This PR
does not do that: **we don't actually know the cause of the violation, and
in health insurance, silently "fixing" financial data you don't understand
is exactly how a data team creates the next wrongful-denial incident while
erasing the evidence of this one.** Clean rows flow, suspect rows are
routed to `staging_patients_quarantine` for a human to review,
nothing is invented. Guardian never pretends to know more than the
evidence supports.

## Validation (deterministic, two required passes — never the real database)

Pass 1 — does the fix clean the data correctly? (scratch COPY of the real,
already-populated healthcare.db)

| Check | Result |
|---|---|
| Original `staging_patients` row count | 55500 |
| Clean rows after fix | 54037 |
| **Quarantined rows** | **1463** |
| Violations remaining in clean table (must be 0) | 0 |
| Conservation (clean + quarantined == original) | PASS |

Pass 2 — does this file still work as a from-scratch build script, the
same job the original file did? (a genuinely fresh database, seeded with
nothing but a small real `raw_patients` sample, run through the ENTIRE
transform sequence)

| Check | Result |
|---|---|
| Fresh-database build | PASS |

## Operational note

Rows in `staging_patients_quarantine` require human review — suggested
owner: **clinical_team** (read live from DataHub ownership metadata).

## Investigator's Finding (verbatim)

> Sentinel flagged Cigna/obesity for a 15.99% denial rate (4.2x baseline). Breaking the 298 denials down by reason code shows 280 (93.96%) are INVALID_BILLING_AMOUNT, with a smaller unrelated remainder of 11 HIGH_RISK_SCORE and 7 RANDOM_AUDIT denials that have no field-level defect to trace by design. For the 280 INVALID_BILLING_AMOUNT claims, billing_amount < 0 was checked at every hop of the confirmed lineage chain (claims -> mart_billing -> staging_patients -> raw_patients), verifying row alignment via rowid at each hop before trusting the count. The negative value reproduces at exactly 280/280 (100%) at every single hop, including raw_patients, which has no further upstream lineage registered. This means the bad data was never introduced by any transform in this pipeline -- it is already present in the earliest available source data. The elevated denial rate for this segment is a downstream symptom of a pre-existing data-quality defect inherited from raw_patients, not a bug introduced by mart_billing, staging_patients, or the claims derivation itself.

## Generated fix SQL

```sql
-- staging_patients — standardizes raw_patients: lowercases/trims a handful
-- of categorical columns, tags every row with a pipeline_status. Does NOT
-- filter or validate anything — data-quality issues in raw_patients (e.g.
-- negative billing_amount, invalid ages) pass through here unchanged and
-- propagate downstream. That's intentional: this table's job is
-- standardization, not quality enforcement.
--
-- Upstream: raw_patients (source ingest, not owned by this transform).
-- Downstream: mart_billing.sql, mart_demographics.sql (the pipeline forks
-- here — same staging_patients row feeds both marts).
--
-- FIX (quarantine billing_amount < 0): Sentinel flagged Cigna/obesity for a
-- 15.99% denial rate (4.2x baseline). 280 of 298 denials (93.96%) are
-- INVALID_BILLING_AMOUNT, and a negative billing_amount reproduces at
-- exactly 280/280 (100%) at every hop of claims -> mart_billing ->
-- staging_patients -> raw_patients, with no further upstream lineage
-- registered past raw_patients. The defect is already present in the
-- earliest available source data -- it was never introduced by
-- mart_billing, staging_patients, or the claims derivation -- so we do NOT
-- correct or guess at a "true" value (no ABS(), no sign flip). Instead,
-- rows failing billing_amount >= 0 (including NULL/unparseable amounts,
-- which can't be shown to be >= 0 either) are routed to
-- staging_patients_quarantine instead of landing in staging_patients
-- unexamined, so they stay visible for follow-up without silently
-- corrupting downstream marts.
--
-- Uses CREATE TABLE IF NOT EXISTS ... WHERE 0 + DELETE + INSERT (never
-- DROP) for both tables, so this same script (a) cleans a database that
-- already has real rows in staging_patients, and (b) still works as a
-- from-scratch build against a completely fresh, empty database. A
-- DROP + recreate would leave any views defined against staging_patients
-- unable to resolve the table mid-script.

CREATE TABLE IF NOT EXISTS staging_patients_quarantine AS
SELECT
    *,
    LOWER(TRIM(gender))            AS gender_clean,
    LOWER(TRIM(blood_type))        AS blood_type_clean,
    LOWER(TRIM(medical_condition)) AS condition_clean,
    LOWER(TRIM(admission_type))    AS admission_type_clean,
    LOWER(TRIM(test_results))      AS test_results_clean,
    'staged'                       AS pipeline_status
FROM raw_patients
WHERE 0;

DELETE FROM staging_patients_quarantine;

INSERT INTO staging_patients_quarantine
SELECT
    *,
    LOWER(TRIM(gender))            AS gender_clean,
    LOWER(TRIM(blood_type))        AS blood_type_clean,
    LOWER(TRIM(medical_condition)) AS condition_clean,
    LOWER(TRIM(admission_type))    AS admission_type_clean,
    LOWER(TRIM(test_results))      AS test_results_clean,
    'staged'                       AS pipeline_status
FROM raw_patients
WHERE (
    billing_amount IS NULL
    OR TRIM(billing_amount) = ''
    OR NOT (
        TRIM(billing_amount) GLOB '-[0-9]*'
        OR TRIM(billing_amount) GLOB '[0-9]*'
    )
    OR CAST(TRIM(billing_amount) AS REAL) < 0
);

CREATE TABLE IF NOT EXISTS staging_patients AS
SELECT
    *,
    LOWER(TRIM(gender))            AS gender_clean,
    LOWER(TRIM(blood_type))        AS blood_type_clean,
    LOWER(TRIM(medical_condition)) AS condition_clean,
    LOWER(TRIM(admission_type))    AS admission_type_clean,
    LOWER(TRIM(test_results))      AS test_results_clean,
    'staged'                       AS pipeline_status
FROM raw_patients
WHERE 0;

DELETE FROM staging_patients;

INSERT INTO staging_patients
SELECT
    *,
    LOWER(TRIM(gender))            AS gender_clean,
    LOWER(TRIM(blood_type))        AS blood_type_clean,
    LOWER(TRIM(medical_condition)) AS condition_clean,
    LOWER(TRIM(admission_type))    AS admission_type_clean,
    LOWER(TRIM(test_results))      AS test_results_clean,
    'staged'                       AS pipeline_status
FROM raw_patients
WHERE NOT (
    billing_amount IS NULL
    OR TRIM(billing_amount) = ''
    OR NOT (
        TRIM(billing_amount) GLOB '-[0-9]*'
        OR TRIM(billing_amount) GLOB '[0-9]*'
    )
    OR CAST(TRIM(billing_amount) AS REAL) < 0
);
```

Full incident record: https://github.com/ThakurRanveerSingh/claims-denial-guardian/blob/main/examples/INC-20260724T234736Z-cigna-obesity/incident.json

