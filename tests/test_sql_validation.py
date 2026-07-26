"""
Tests for src/codegen/sql_validation.py (docs/decisions/0008-remediator-design.md).

Pure, deterministic, no LLM, no network, no git — every test here uses a
small synthetic sqlite database, not the real healthcare.db. Speed and
isolation, not a live-data proof (that's what Remediator's own live test
covers, against the real committed db via scratch_copy()).
"""

import sqlite3
from pathlib import Path

import pytest

from codegen.sql_validation import ValidationResult, apply_and_validate_fix, scratch_copy


@pytest.fixture
def source_db(tmp_path) -> Path:
    """A tiny, real, file-backed sqlite db with a `claims`-shaped table:
    5 rows, 2 negative (billing_amount REAL, matching claims' real type)."""
    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE claims (claim_id TEXT PRIMARY KEY, billing_amount REAL)")
    conn.executemany(
        "INSERT INTO claims VALUES (?, ?)",
        [("CLM-1", 100.0), ("CLM-2", -50.0), ("CLM-3", 200.0), ("CLM-4", -75.0), ("CLM-5", 300.0)],
    )
    conn.commit()
    conn.close()
    return db_path


CORRECT_FIX_SQL = """
CREATE TABLE claims_new AS SELECT * FROM claims WHERE billing_amount >= 0;
CREATE TABLE claims_quarantine AS SELECT * FROM claims WHERE billing_amount < 0;
DROP TABLE claims;
ALTER TABLE claims_new RENAME TO claims;
"""

# Only catches ONE of the two negative rows -- a deliberately incomplete fix.
INCOMPLETE_FIX_SQL = """
CREATE TABLE claims_new AS SELECT * FROM claims WHERE billing_amount >= 0 OR claim_id = 'CLM-2';
CREATE TABLE claims_quarantine AS SELECT * FROM claims WHERE billing_amount < 0 AND claim_id != 'CLM-2';
DROP TABLE claims;
ALTER TABLE claims_new RENAME TO claims;
"""

# Splits correctly but silently drops CLM-5 -- violates conservation, not the violation check.
DROPS_A_ROW_FIX_SQL = """
CREATE TABLE claims_new AS SELECT * FROM claims WHERE billing_amount >= 0 AND claim_id != 'CLM-5';
CREATE TABLE claims_quarantine AS SELECT * FROM claims WHERE billing_amount < 0;
DROP TABLE claims;
ALTER TABLE claims_new RENAME TO claims;
"""

SYNTAX_ERROR_SQL = "CREATE TABLE claims_new AS SELCT * FROM claims;"  # typo: SELCT

WRONG_TABLE_NAMES_SQL = """
CREATE TABLE something_else AS SELECT * FROM claims WHERE billing_amount >= 0;
"""


class TestScratchCopy:
    def test_creates_a_real_independent_copy(self, source_db, tmp_path):
        with scratch_copy(source_db) as scratch_path:
            assert scratch_path.exists()
            assert scratch_path != source_db
            conn = sqlite3.connect(str(scratch_path))
            count = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            conn.close()
            assert count == 5

    def test_mutating_the_scratch_copy_never_touches_the_source(self, source_db):
        with scratch_copy(source_db) as scratch_path:
            conn = sqlite3.connect(str(scratch_path))
            conn.execute("DELETE FROM claims")
            conn.commit()
            conn.close()

        # Source, opened fresh, still has all 5 rows -- the mutation was
        # only ever applied to the scratch copy.
        source_conn = sqlite3.connect(str(source_db))
        source_count = source_conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        source_conn.close()
        assert source_count == 5

    def test_cleans_up_after_itself(self, source_db):
        with scratch_copy(source_db) as scratch_path:
            captured_path = scratch_path
        assert not captured_path.exists()
        assert not captured_path.parent.exists()

    def test_cleans_up_even_on_exception(self, source_db):
        captured_path = None
        with pytest.raises(ValueError):
            with scratch_copy(source_db) as scratch_path:
                captured_path = scratch_path
                raise ValueError("simulated failure mid-fix")
        assert not captured_path.exists()


class TestApplyAndValidateFix:
    def test_correct_fix_succeeds(self, source_db):
        with scratch_copy(source_db) as scratch_path:
            result = apply_and_validate_fix(
                scratch_path, CORRECT_FIX_SQL,
                original_table="claims", clean_table="claims", quarantine_table="claims_quarantine",
            )
        assert result.success is True
        assert result.original_count == 5
        assert result.clean_count == 3
        assert result.quarantine_count == 2
        assert result.violation_count_in_clean == 0
        assert result.conserves_rows is True
        assert result.error is None

    def test_incomplete_fix_fails_on_violation_count(self, source_db):
        with scratch_copy(source_db) as scratch_path:
            result = apply_and_validate_fix(
                scratch_path, INCOMPLETE_FIX_SQL,
                original_table="claims", clean_table="claims", quarantine_table="claims_quarantine",
            )
        assert result.success is False
        assert result.violation_count_in_clean == 1  # CLM-2 snuck into "clean"
        assert result.conserves_rows is True  # conservation is fine; the OTHER check caught this

    def test_row_dropping_fix_fails_on_conservation(self, source_db):
        """The check the repo owner specifically praised: a fix with zero
        violations in the clean table can still be wrong if it silently
        drops a row instead of routing it anywhere."""
        with scratch_copy(source_db) as scratch_path:
            result = apply_and_validate_fix(
                scratch_path, DROPS_A_ROW_FIX_SQL,
                original_table="claims", clean_table="claims", quarantine_table="claims_quarantine",
            )
        assert result.violation_count_in_clean == 0  # would pass a violation-only check
        assert result.conserves_rows is False  # but conservation catches the dropped CLM-5
        assert result.success is False

    def test_sql_syntax_error_is_reported_not_raised(self, source_db):
        with scratch_copy(source_db) as scratch_path:
            result = apply_and_validate_fix(
                scratch_path, SYNTAX_ERROR_SQL,
                original_table="claims", clean_table="claims", quarantine_table="claims_quarantine",
            )
        assert result.success is False
        assert result.error is not None
        assert result.clean_count is None  # never got far enough to count

    def test_wrong_table_names_reported_as_error_not_crash(self, source_db):
        with scratch_copy(source_db) as scratch_path:
            result = apply_and_validate_fix(
                scratch_path, WRONG_TABLE_NAMES_SQL,
                original_table="claims", clean_table="claims", quarantine_table="claims_quarantine",
            )
        assert result.success is False
        assert result.error is not None
        assert "claims_quarantine" in result.error or "no such table" in result.error.lower()

    def test_text_billing_amount_column_compared_numerically_not_lexically(self, tmp_path):
        """staging_patients' real billing_amount column is TEXT, not REAL --
        a bare `< 0` there would do a STRING comparison (e.g. "-50" < "0" is
        actually true lexically by coincidence, but "9" < "0" would be
        false lexically despite 9 not being negative -- the point is this
        must not depend on lexical luck). CAST(... AS REAL) is what makes
        the check correct regardless of column type."""
        db_path = tmp_path / "text_billing.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE staging (id TEXT PRIMARY KEY, billing_amount TEXT)")
        conn.executemany(
            "INSERT INTO staging VALUES (?, ?)",
            [("A", "100.0"), ("B", "-50.0"), ("C", "9.0")],
        )
        conn.commit()
        conn.close()

        fix_sql = """
        CREATE TABLE staging_new AS SELECT * FROM staging WHERE CAST(billing_amount AS REAL) >= 0;
        CREATE TABLE staging_quarantine AS SELECT * FROM staging WHERE CAST(billing_amount AS REAL) < 0;
        DROP TABLE staging;
        ALTER TABLE staging_new RENAME TO staging;
        """
        with scratch_copy(db_path) as scratch_path:
            result = apply_and_validate_fix(
                scratch_path, fix_sql,
                original_table="staging", clean_table="staging", quarantine_table="staging_quarantine",
                check_column="billing_amount",
            )
        assert result.success is True
        assert result.clean_count == 2  # A and C
        assert result.quarantine_count == 1  # B
