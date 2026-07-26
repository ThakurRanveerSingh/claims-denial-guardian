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
