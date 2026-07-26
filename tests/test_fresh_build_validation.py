"""
Tests for src/codegen/fresh_build_validation.py
(docs/decisions/0008-remediator-design.md's amendment: the Part D
regression this module exists to catch).

Pure, deterministic, no LLM, no network, no git -- synthetic small sqlite
databases and synthetic transform/*.sql fixture files under tmp_path, not
the real healthcare.db or the real denial-guardian-data-platform repo.
Fixture files deliberately use the REAL stage names (staging_patients,
mart_billing, mart_demographics, claims) since TRANSFORM_ORDER is a fixed
module constant tied to the real pipeline, not a parameter -- there is
nothing project-specific in this module worth hiding behind a fake stage
naming scheme.
"""

import sqlite3
from pathlib import Path

import pytest

from codegen.fresh_build_validation import (
    TRANSFORM_ORDER,
    FreshBuildResult,
    fresh_scratch_db,
    run_fresh_build,
    seed_fresh_raw_patients,
)


# ===========================================================================
# Fixtures.
# ===========================================================================


@pytest.fixture
def fake_real_db(tmp_path) -> Path:
    """A tiny, real, file-backed sqlite db standing in for healthcare.db --
    just enough for seed_fresh_raw_patients to read a real DDL string and
    real sample rows from. billing_amount is TEXT, matching the real
    raw_patients column type."""
    db_path = tmp_path / "fake_healthcare.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute('CREATE TABLE raw_patients ("name" TEXT, "billing_amount" TEXT)')
    conn.executemany(
        "INSERT INTO raw_patients VALUES (?, ?)",
        [("Alice", "100.0"), ("Bob", "200.0"), ("Carol", "-50.0"), ("Dave", "-75.0"), ("Eve", "300.0")],
    )
    conn.commit()
    conn.close()
    return db_path


# Fresh-db-safe fixture transform files, using the CREATE TABLE IF NOT
# EXISTS ... WHERE 0 idiom this module's docstring/remediator.py's prompt
# both point generated fixes toward.
FRESH_SAFE_STAGING_PATIENTS_SQL = """
CREATE TABLE IF NOT EXISTS staging_patients AS SELECT * FROM raw_patients WHERE 0;
DELETE FROM staging_patients;
INSERT INTO staging_patients SELECT * FROM raw_patients;
"""

FRESH_SAFE_MART_BILLING_SQL = """
CREATE TABLE IF NOT EXISTS mart_billing AS SELECT * FROM staging_patients WHERE 0;
DELETE FROM mart_billing;
INSERT INTO mart_billing SELECT * FROM staging_patients;
"""

FRESH_SAFE_MART_DEMOGRAPHICS_SQL = """
CREATE TABLE IF NOT EXISTS mart_demographics AS SELECT * FROM staging_patients WHERE 0;
DELETE FROM mart_demographics;
INSERT INTO mart_demographics SELECT * FROM staging_patients;
"""

FRESH_SAFE_CLAIMS_SQL = """
CREATE TABLE IF NOT EXISTS claims AS SELECT * FROM mart_billing WHERE 0;
CREATE TABLE IF NOT EXISTS claims_quarantine AS SELECT * FROM mart_billing WHERE 0;
DELETE FROM claims;
DELETE FROM claims_quarantine;
INSERT INTO claims SELECT * FROM mart_billing WHERE CAST(billing_amount AS REAL) >= 0;
INSERT INTO claims_quarantine SELECT * FROM mart_billing WHERE CAST(billing_amount AS REAL) < 0;
"""

# The REAL bug found live during Part D, reproduced in miniature: assumes
# `claims` already exists (DROP TABLE with no prior CREATE) -- fails on a
# genuinely fresh database regardless of what any other stage's fix does.
BROKEN_CLAIMS_ASSUMES_PRIOR_EXISTENCE_SQL = """
CREATE TABLE claims_new AS SELECT * FROM mart_billing WHERE CAST(billing_amount AS REAL) >= 0;
CREATE TABLE claims_quarantine AS SELECT * FROM mart_billing WHERE CAST(billing_amount AS REAL) < 0;
DROP TABLE claims;
ALTER TABLE claims_new RENAME TO claims;
"""

# A candidate fix that runs fine end-to-end but forgets to filter --
# structurally "successful" (no SQL error) yet leaves violations, the
# distinct failure mode the post-sequence violation check exists to catch.
CANDIDATE_STAGING_PATIENTS_FORGETS_TO_FILTER_ANYTHING_SQL = """
CREATE TABLE IF NOT EXISTS staging_patients AS SELECT * FROM raw_patients WHERE 0;
CREATE TABLE IF NOT EXISTS staging_patients_quarantine AS SELECT * FROM raw_patients WHERE 0;
DELETE FROM staging_patients;
INSERT INTO staging_patients SELECT * FROM raw_patients;
"""

# The REAL bug found live during Part D, verbatim in shape: no CREATE at
# all, assumes staging_patients already has data.
CANDIDATE_STAGING_PATIENTS_ASSUMES_PRIOR_EXISTENCE_SQL = """
CREATE TABLE staging_patients_quarantine AS SELECT * FROM raw_patients WHERE CAST(billing_amount AS REAL) < 0;
DELETE FROM staging_patients;
INSERT INTO staging_patients SELECT * FROM raw_patients WHERE CAST(billing_amount AS REAL) >= 0;
"""


def _write_transform_files(repo_path: Path, **overrides: str) -> None:
    """Write the 4 real-named transform files into repo_path/transform/,
    using FRESH_SAFE_* as the default body for whichever stage isn't
    passed in `overrides`."""
    defaults = {
        "staging_patients": FRESH_SAFE_STAGING_PATIENTS_SQL,
        "mart_billing": FRESH_SAFE_MART_BILLING_SQL,
        "mart_demographics": FRESH_SAFE_MART_DEMOGRAPHICS_SQL,
        "claims": FRESH_SAFE_CLAIMS_SQL,
    }
    defaults.update(overrides)
    transform_dir = repo_path / "transform"
    transform_dir.mkdir(parents=True, exist_ok=True)
    for stage, sql in defaults.items():
        (transform_dir / f"{stage}.sql").write_text(sql)


# ===========================================================================
# 1. fresh_scratch_db -- cleanup discipline, same shape as scratch_copy's.
# ===========================================================================


class TestFreshScratchDb:
    def test_yields_a_path_that_does_not_yet_exist_as_a_file(self):
        with fresh_scratch_db() as path:
            assert not path.exists()  # nothing has created it yet -- caller does that
            assert path.parent.exists()

    def test_cleans_up_after_itself(self):
        with fresh_scratch_db() as path:
            path.touch()
            captured = path
        assert not captured.exists()
        assert not captured.parent.exists()

    def test_cleans_up_even_on_exception(self):
        captured = None
        with pytest.raises(ValueError):
            with fresh_scratch_db() as path:
                captured = path
                raise ValueError("simulated failure")
        assert not captured.parent.exists()


# ===========================================================================
# 2. seed_fresh_raw_patients.
# ===========================================================================


class TestSeedFreshRawPatients:
    def test_creates_raw_patients_with_real_ddl_and_seeds_both_kinds_of_row(self, fake_real_db, tmp_path):
        scratch_path = tmp_path / "scratch.db"
        seed_fresh_raw_patients(scratch_path, fake_real_db, sample_size=2)

        conn = sqlite3.connect(str(scratch_path))
        try:
            total = conn.execute("SELECT COUNT(*) FROM raw_patients").fetchone()[0]
            negative = conn.execute("SELECT COUNT(*) FROM raw_patients WHERE CAST(billing_amount AS REAL) < 0").fetchone()[0]
            clean = conn.execute("SELECT COUNT(*) FROM raw_patients WHERE CAST(billing_amount AS REAL) >= 0").fetchone()[0]
        finally:
            conn.close()

        assert total == 4  # 2 clean + 2 negative, per sample_size=2
        assert negative == 2
        assert clean == 2

    def test_scratch_db_starts_with_only_raw_patients_no_other_tables(self, fake_real_db, tmp_path):
        scratch_path = tmp_path / "scratch.db"
        seed_fresh_raw_patients(scratch_path, fake_real_db, sample_size=1)

        conn = sqlite3.connect(str(scratch_path))
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()
        assert tables == {"raw_patients"}

    def test_never_mutates_the_real_source_db(self, fake_real_db, tmp_path):
        scratch_path = tmp_path / "scratch.db"
        seed_fresh_raw_patients(scratch_path, fake_real_db, sample_size=2)

        conn = sqlite3.connect(str(fake_real_db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM raw_patients").fetchone()[0]
        finally:
            conn.close()
        assert count == 5  # unchanged from the fixture's original 5 rows


# ===========================================================================
# 3. run_fresh_build.
# ===========================================================================


class TestRunFreshBuild:
    def test_correct_fresh_safe_fix_succeeds_end_to_end(self, fake_real_db, tmp_path):
        repo_path = tmp_path / "data-platform"
        _write_transform_files(repo_path)  # all 4 stages fresh-safe, including the fix's own file

        with fresh_scratch_db() as scratch_path:
            seed_fresh_raw_patients(scratch_path, fake_real_db, sample_size=3)
            result = run_fresh_build(scratch_path, "claims", FRESH_SAFE_CLAIMS_SQL, repo_path)

        assert result.success is True
        assert result.violation_count_in_clean == 0
        assert result.quarantine_row_count is not None and result.quarantine_row_count > 0

    def test_candidate_fix_that_assumes_prior_existence_fails_at_its_own_stage(self, fake_real_db, tmp_path):
        """The exact bug shape found live during Part D: no CREATE at all."""
        repo_path = tmp_path / "data-platform"
        _write_transform_files(repo_path)  # other 3 stages are fine; staging_patients is what we're testing

        with fresh_scratch_db() as scratch_path:
            seed_fresh_raw_patients(scratch_path, fake_real_db, sample_size=3)
            result = run_fresh_build(scratch_path, "staging_patients", CANDIDATE_STAGING_PATIENTS_ASSUMES_PRIOR_EXISTENCE_SQL, repo_path)

        assert result.success is False
        assert result.failed_stage == "staging_patients"
        assert "no such table" in (result.error or "").lower()

    def test_a_real_unmodified_downstream_file_with_the_bug_fails_the_sequence_too(self, fake_real_db, tmp_path):
        """The point the repo owner raised directly: PR #1's own claims.sql
        (DROP TABLE claims with no prior CREATE) breaks a fresh build
        regardless of what any OTHER stage's fix does -- this must be
        caught even when claims itself isn't the stage under test."""
        repo_path = tmp_path / "data-platform"
        _write_transform_files(repo_path, claims=BROKEN_CLAIMS_ASSUMES_PRIOR_EXISTENCE_SQL)

        with fresh_scratch_db() as scratch_path:
            seed_fresh_raw_patients(scratch_path, fake_real_db, sample_size=3)
            # staging_patients is the stage under test (fine, fresh-safe);
            # claims is a REAL, unmodified, currently-broken dependency.
            result = run_fresh_build(scratch_path, "staging_patients", FRESH_SAFE_STAGING_PATIENTS_SQL, repo_path)

        assert result.success is False
        assert result.failed_stage == "claims"
        assert "no such table" in (result.error or "").lower()

    def test_fix_that_runs_clean_but_leaves_violations_is_caught_structurally(self, fake_real_db, tmp_path):
        """Distinct failure mode from a SQL error: the sequence completes
        without any exception, but the fix stage's own output still has
        rows violating the check -- e.g. a fix that forgot to filter."""
        repo_path = tmp_path / "data-platform"
        _write_transform_files(repo_path)

        with fresh_scratch_db() as scratch_path:
            seed_fresh_raw_patients(scratch_path, fake_real_db, sample_size=3)
            result = run_fresh_build(
                scratch_path, "staging_patients", CANDIDATE_STAGING_PATIENTS_FORGETS_TO_FILTER_ANYTHING_SQL, repo_path,
            )

        assert result.success is False
        assert result.failed_stage is None  # it executed fine -- this is the structural check, not a SQL error
        assert result.violation_count_in_clean is not None and result.violation_count_in_clean > 0

    def test_missing_quarantine_table_is_reported_not_raised(self, fake_real_db, tmp_path):
        repo_path = tmp_path / "data-platform"
        _write_transform_files(repo_path)
        candidate_forgets_quarantine_table = """
        CREATE TABLE IF NOT EXISTS staging_patients AS SELECT * FROM raw_patients WHERE 0;
        DELETE FROM staging_patients;
        INSERT INTO staging_patients SELECT * FROM raw_patients WHERE CAST(billing_amount AS REAL) >= 0;
        """
        with fresh_scratch_db() as scratch_path:
            seed_fresh_raw_patients(scratch_path, fake_real_db, sample_size=3)
            result = run_fresh_build(scratch_path, "staging_patients", candidate_forgets_quarantine_table, repo_path)

        assert result.success is False
        assert result.error is not None
        assert "quarantine" in result.error.lower() or "no such table" in result.error.lower()

    def test_transform_order_is_the_real_fixed_pipeline_order(self):
        assert TRANSFORM_ORDER == ["staging_patients", "mart_billing", "mart_demographics", "claims"]
