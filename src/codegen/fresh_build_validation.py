#!/usr/bin/env python3
"""
fresh_build_validation.py — the SECOND, harder validation pass Remediator
runs before treating any fix as ready for a PR. Design:
docs/decisions/0008-remediator-design.md's amendment (Sprint 3 WP2 Part D
regression).

Why this module exists, stated plainly: sql_validation.py's
apply_and_validate_fix() answers "does this fix clean the data correctly?"
by running the candidate SQL against a scratch COPY of the real,
already-populated healthcare.db. That is a real, necessary check -- but it
is not the same question a human reviewer asks before merging a file:
"does this file still do everything the old file did?" Part D's first live
run produced a Cigna/obesity fix that passed apply_and_validate_fix
cleanly (violation count 0, conservation PASS) while silently dropping its
own CREATE TABLE statement -- the generated `staging_patients.sql` opened
with `DELETE FROM staging_patients`, which requires the table to already
exist. Every scratch copy apply_and_validate_fix ever tests against
already has that table, built by an earlier real run -- so the check had
no way to see the file could no longer bootstrap itself on a genuinely
fresh database. Fresh-database runnability was a real invariant of the
original files that nobody had written down as a check, so nothing
guarded it.

This module runs the ENTIRE transform sequence -- staging_patients ->
mart_billing -> mart_demographics -> claims, the pipeline's one fixed
dependency order -- against a scratch database seeded with NOTHING but a
small, real sample of raw_patients rows. The stage under test runs its
CANDIDATE (generated) SQL; every other stage runs its real, currently-
committed transform/<stage>.sql from the data-platform repo. If the whole
sequence completes without error, the fix is proven self-sufficient as a
from-scratch build step, not merely correct on top of a database some
earlier, unrelated run already built. No LLM anywhere in this module,
same "LLM proposes, code verifies" split as sql_validation.py.
"""

import contextlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# The pipeline's one fixed build order (denial-guardian-data-platform's
# transform/ directory) -- not derived dynamically, because this is a
# small, non-branching, already-fully-known pipeline (Sprint 3 WP2 Part A
# scaffolded exactly these four files, no others). staging_patients has no
# entry here because it depends only on raw_patients, which this module
# seeds directly rather than building via any transform file.
TRANSFORM_ORDER = ["staging_patients", "mart_billing", "mart_demographics", "claims"]


@dataclass
class FreshBuildResult:
    """The verdict on one fresh-database build attempt.

    `failed_stage`/`error`: which transform/<stage>.sql failed to execute
    and why -- distinguishes "the candidate fix itself is broken" from "a
    real, unmodified downstream/upstream file broke" (the latter happened
    for real during Part D: claims.sql's own DROP TABLE claims assumes
    claims already exists, which is untrue on a genuinely fresh database,
    independent of whatever fix is under test).

    `violation_count_in_clean`/`quarantine_row_count`: a lightweight sanity
    check on the FIX STAGE's own resulting tables after the full sequence
    completes -- catches a fix whose fresh-database bootstrap branch (e.g.
    a CREATE TABLE IF NOT EXISTS path) silently produces different rows
    than its already-populated-database branch, a distinct failure mode
    from "does it merely execute without a SQL error." `None` when the
    sequence never reached the fix stage, or reached it but the stage
    itself errored (see `failed_stage`).
    """

    success: bool
    failed_stage: Optional[str] = None
    error: Optional[str] = None
    violation_count_in_clean: Optional[int] = None
    quarantine_row_count: Optional[int] = None


@contextlib.contextmanager
def fresh_scratch_db() -> Iterator[Path]:
    """A brand-new, completely empty sqlite file -- not a copy of anything.
    Same try/finally whole-temp-dir cleanup discipline as
    sql_validation.scratch_copy(), for the same reason: nothing in this
    module should leave a stray file behind, success or failure."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="guardian-fresh-build-"))
    db_path = tmp_dir / "fresh.db"
    try:
        yield db_path
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


def seed_fresh_raw_patients(scratch_db_path: Path, real_healthcare_db_path: Path, sample_size: int = 4) -> None:
    """Create `raw_patients` in `scratch_db_path` (a brand-new, otherwise
    completely empty database -- no staging_patients, no marts, no claims,
    no views) using the REAL live DDL read from the real database's own
    sqlite_master (never hardcoded -- same "read the schema, don't assume
    it" discipline as everywhere else DataHub/db schema facts are used in
    this codebase), then seed it with `sample_size` REAL clean rows and
    `sample_size` REAL billing_amount-negative rows pulled directly from
    the real table. Real, not fabricated: the seed data needs to actually
    exercise the quarantine split meaningfully, and sampling genuine rows
    is both simpler and more honest than inventing synthetic ones.

    Read-only against `real_healthcare_db_path` -- opens it, selects from
    it, closes it. Never writes to it, same standing constraint every
    other module touching the real healthcare.db observes.
    """
    real_conn = sqlite3.connect(f"file:{real_healthcare_db_path}?mode=ro", uri=True)
    try:
        ddl_row = real_conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='raw_patients'").fetchone()
        if ddl_row is None:
            raise RuntimeError("raw_patients table not found in the real healthcare.db")
        raw_patients_ddl = ddl_row[0]

        clean_rows = real_conn.execute(
            "SELECT * FROM raw_patients WHERE CAST(billing_amount AS REAL) >= 0 LIMIT ?", (sample_size,)
        ).fetchall()
        negative_rows = real_conn.execute(
            "SELECT * FROM raw_patients WHERE CAST(billing_amount AS REAL) < 0 LIMIT ?", (sample_size,)
        ).fetchall()
        column_count = len(real_conn.execute("SELECT * FROM raw_patients LIMIT 1").description)
    finally:
        real_conn.close()

    scratch_conn = sqlite3.connect(str(scratch_db_path))
    try:
        scratch_conn.execute(raw_patients_ddl)
        placeholders = ",".join(["?"] * column_count)
        scratch_conn.executemany(f"INSERT INTO raw_patients VALUES ({placeholders})", clean_rows + negative_rows)
        scratch_conn.commit()
    finally:
        scratch_conn.close()


def run_fresh_build(
    scratch_db_path: Path,
    fix_target_table: str,
    candidate_sql: str,
    data_platform_repo_path: Path,
    *,
    check_column: str = "billing_amount",
) -> FreshBuildResult:
    """Run every stage in TRANSFORM_ORDER against `scratch_db_path` (which
    must already have ONLY `raw_patients` in it -- see
    seed_fresh_raw_patients). The `fix_target_table` stage runs
    `candidate_sql`; every other stage reads and runs its real, current
    `transform/<stage>.sql` from `data_platform_repo_path`. Stops and
    reports the first stage that fails to execute at all.

    After a fully successful run, checks the fix stage's OWN resulting
    tables: zero rows in `fix_target_table` may violate
    `check_column >= 0`, and `fix_target_table_quarantine` must exist and
    be queryable. This is deliberately NOT a conservation/count check
    against a "before" state the way apply_and_validate_fix's is -- there
    is no meaningful "original row count" here (the fix stage may not even
    be the first thing built from raw_patients, e.g. `claims`), so this
    pass checks structural correctness of the split, not row-for-row
    accounting; apply_and_validate_fix already owns that job on the
    full-scale, already-populated database.
    """
    conn = sqlite3.connect(str(scratch_db_path))
    try:
        for stage in TRANSFORM_ORDER:
            if stage == fix_target_table:
                stage_sql = candidate_sql
            else:
                stage_path = data_platform_repo_path / "transform" / f"{stage}.sql"
                stage_sql = stage_path.read_text()

            try:
                conn.executescript(stage_sql)
            except sqlite3.Error as e:
                return FreshBuildResult(success=False, failed_stage=stage, error=str(e))

        try:
            violation_count = conn.execute(
                f"SELECT COUNT(*) FROM {fix_target_table} WHERE CAST({check_column} AS REAL) < 0"
            ).fetchone()[0]
            quarantine_count = conn.execute(f"SELECT COUNT(*) FROM {fix_target_table}_quarantine").fetchone()[0]
        except sqlite3.Error as e:
            return FreshBuildResult(success=False, failed_stage=fix_target_table, error=str(e))

        return FreshBuildResult(
            success=(violation_count == 0),
            violation_count_in_clean=violation_count,
            quarantine_row_count=quarantine_count,
        )
    finally:
        conn.close()
