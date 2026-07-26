-- claims — one row per admission/billing record (this pipeline has no
-- separate "claim" concept upstream; a mart_billing row IS a claim).
-- claim_id is derived deterministically from mart_billing's rowid
-- ('CLM-<rowid, zero-padded>'), not a random UUID — traceable straight
-- back to the source row.
--
-- medical_condition is joined in from mart_demographics via rowid, not
-- name/hospital: rowid is a guaranteed 1:1 correspondence since both marts
-- derive from staging_patients with no filtering/aggregation; a name+
-- hospital join was measured to collide on a meaningful fraction of rows
-- and was rejected for that reason.
--
-- Upstream: mart_billing.sql, mart_demographics.sql (two real upstreams).
-- Downstream: denials, denial_model_scores (owned by the Guardian side,
-- not this repo).
--
-- QUARANTINE FIX (Sentinel finding: UnitedHealthcare/diabetes denial rate
-- 5.7x baseline). 361/375 flagged denials are INVALID_BILLING_AMOUNT; the
-- other two reason codes (RANDOM_AUDIT, HIGH_RISK_SCORE) have no
-- field-level defect to trace and are out of scope for this fix. Walking
-- claims -> mart_billing -> staging_patients -> raw_patients found two
-- non-overlapping causes, neither of which this script attempts to
-- correct (the mechanism isn't proven safe to reverse programmatically):
--   (1) 325 rows: mart_billing holds the correct positive billing_amount,
--       but this build's own INSERT was landing its exact negation with
--       nothing in the SELECT above to explain the flip -- a defect in
--       claims-build logic itself, not upstream.
--   (2) 36 rows: already negative as far back as raw_patients, the root
--       of the lineage -- a pre-existing source-data defect passed
--       through unchanged (mart_billing/staging_patients verified as
--       exact passthroughs, whole-table).
-- Rather than guess which rows are which or flip signs, every row is
-- classified purely by the observable rule: clean iff billing_amount is
-- non-NULL, numeric, and >= 0. Anything else (negative, NULL, or
-- non-numeric/unparseable) is quarantined instead of landing in claims
-- unexamined. Same upstream join as the original build, split two ways.
-- Both tables use CREATE TABLE IF NOT EXISTS ... AS SELECT ... WHERE 0 so
-- this script works unchanged on a from-scratch database and on one that
-- already has claims data -- no DROP, so views defined against claims
-- never see it missing mid-script.

CREATE TABLE IF NOT EXISTS claims_quarantine AS
SELECT
    'CLM-' || printf('%06d', b.rowid)  AS claim_id,
    b.rowid                            AS source_billing_rowid,
    b.name                              AS patient_name,
    b.hospital                          AS hospital,
    b.insurance_provider                AS insurance_provider,
    d.medical_condition                 AS medical_condition,
    b.admission_type                    AS admission_type,
    b.billing_amount                    AS billing_amount,
    b.date_of_admission                 AS date_of_admission,
    b.discharge_date                    AS discharge_date,
    b.length_of_stay_days               AS length_of_stay_days,
    b.medication                        AS medication
FROM mart_billing b
JOIN mart_demographics d ON d.rowid = b.rowid
WHERE 0;

CREATE TABLE IF NOT EXISTS claims AS
SELECT
    'CLM-' || printf('%06d', b.rowid)  AS claim_id,
    b.rowid                            AS source_billing_rowid,
    b.name                              AS patient_name,
    b.hospital                          AS hospital,
    b.insurance_provider                AS insurance_provider,
    d.medical_condition                 AS medical_condition,
    b.admission_type                    AS admission_type,
    b.billing_amount                    AS billing_amount,
    b.date_of_admission                 AS date_of_admission,
    b.discharge_date                    AS discharge_date,
    b.length_of_stay_days               AS length_of_stay_days,
    b.medication                        AS medication
FROM mart_billing b
JOIN mart_demographics d ON d.rowid = b.rowid
WHERE 0;

DELETE FROM claims_quarantine;
DELETE FROM claims;

INSERT INTO claims_quarantine (
    claim_id, source_billing_rowid, patient_name, hospital, insurance_provider,
    medical_condition, admission_type, billing_amount, date_of_admission,
    discharge_date, length_of_stay_days, medication
)
SELECT
    'CLM-' || printf('%06d', b.rowid),
    b.rowid,
    b.name,
    b.hospital,
    b.insurance_provider,
    d.medical_condition,
    b.admission_type,
    b.billing_amount,
    b.date_of_admission,
    b.discharge_date,
    b.length_of_stay_days,
    b.medication
FROM mart_billing b
JOIN mart_demographics d ON d.rowid = b.rowid
WHERE NOT (
    b.billing_amount IS NOT NULL
    AND typeof(b.billing_amount) IN ('integer', 'real')
    AND b.billing_amount >= 0
);

INSERT INTO claims (
    claim_id, source_billing_rowid, patient_name, hospital, insurance_provider,
    medical_condition, admission_type, billing_amount, date_of_admission,
    discharge_date, length_of_stay_days, medication
)
SELECT
    'CLM-' || printf('%06d', b.rowid),
    b.rowid,
    b.name,
    b.hospital,
    b.insurance_provider,
    d.medical_condition,
    b.admission_type,
    b.billing_amount,
    b.date_of_admission,
    b.discharge_date,
    b.length_of_stay_days,
    b.medication
FROM mart_billing b
JOIN mart_demographics d ON d.rowid = b.rowid
WHERE b.billing_amount IS NOT NULL
    AND typeof(b.billing_amount) IN ('integer', 'real')
    AND b.billing_amount >= 0;
