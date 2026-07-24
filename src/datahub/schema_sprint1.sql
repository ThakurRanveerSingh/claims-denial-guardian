-- Sprint 1 schema extension: claims, denials, denial_model_scores.
-- Built on top of the existing mart_billing / mart_demographics tables.
-- Design rationale: docs/architecture/lld-sprint1.md §1.
--
-- Idempotent by design (DROP + CREATE): this is a derived/generated layer,
-- not source data, so it's safe to tear down and rebuild on every run —
-- that's what keeps the whole Sprint 1 dataset reproducible end to end.
-- Children dropped before parents to respect the claim_id references,
-- even though SQLite doesn't enforce FKs unless PRAGMA foreign_keys=ON.

DROP TABLE IF EXISTS denial_model_scores;
DROP TABLE IF EXISTS denials;
DROP TABLE IF EXISTS claims;

CREATE TABLE claims (
    claim_id              TEXT PRIMARY KEY,   -- 'CLM-' || printf('%06d', source_billing_rowid)
    source_billing_rowid  INTEGER NOT NULL,   -- mart_billing.rowid this claim was derived from
    patient_name          TEXT,               -- NOT a patient ID; only identity signal the source has
    hospital              TEXT NOT NULL,
    insurance_provider    TEXT NOT NULL,
    medical_condition     TEXT,               -- joined from mart_demographics via rowid
    admission_type        TEXT,
    billing_amount        REAL NOT NULL,
    date_of_admission     TEXT,
    discharge_date        TEXT,
    length_of_stay_days   REAL,
    medication            TEXT
);

-- FK constraints named explicitly (CONSTRAINT ... FOREIGN KEY, not inline
-- REFERENCES): SQLAlchemy reflection leaves an inline REFERENCES constraint's
-- name as NULL, and this DataHub version's Avro schema for
-- ForeignKeyConstraintClass requires a non-null name — an unnamed FK breaks
-- ingestion with an AvroTypeException. Named constraints fix it.
CREATE TABLE denials (
    denial_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id             TEXT NOT NULL,
    denial_date           TEXT NOT NULL,
    denial_reason_code     TEXT NOT NULL,   -- INVALID_BILLING_AMOUNT | HIGH_RISK_SCORE | RANDOM_AUDIT
    denial_amount            REAL NOT NULL, -- Sprint 1: always = claims.billing_amount (full denial only)
    CONSTRAINT fk_denials_claim_id FOREIGN KEY (claim_id) REFERENCES claims(claim_id)
);

CREATE TABLE denial_model_scores (
    score_id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id                       TEXT NOT NULL,
    model_version                  TEXT NOT NULL,   -- 'toy-denial-risk-v1'
    risk_score                     REAL NOT NULL,   -- 0..1
    segment_denial_rate_feature    REAL,            -- raw feature, stored for future drift checks (US-6)
    billing_zscore_feature         REAL,            -- raw feature, stored for future drift checks (US-6)
    scored_at                      TEXT NOT NULL,
    CONSTRAINT fk_denial_model_scores_claim_id FOREIGN KEY (claim_id) REFERENCES claims(claim_id)
);

-- Populate claims: one row per mart_billing row, joined to mart_demographics
-- via rowid (not name/hospital — LLD §1 measured a ~10.7% collision rate on
-- that join) to pull in medical_condition, one of the two segment dimensions
-- the scoring model needs.
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
JOIN mart_demographics d ON d.rowid = b.rowid;
