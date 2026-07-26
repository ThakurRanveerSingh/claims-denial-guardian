#!/usr/bin/env python3
"""
sql_validation.py — deterministic, LLM-free SQL validation against a
throwaway scratch copy of healthcare.db. Design: docs/decisions/0008-remediator-design.md.

NEVER touches the committed healthcare.db — every function here operates
on a temp-file copy, deleted when done. This is the exact discipline this
project learned the hard way: Sprint 3 WP1 Part A included an accidental
real mutation of the committed db (caught via `git diff --stat`, reverted
via `git checkout --`). `scratch_copy()` below is the one, sole way any
caller in this codebase gets a writable path to a healthcare.db-shaped
file — there is no other path in this module that touches the real file.

No LLM anywhere in this module. Remediator (src/agents/remediator.py)
generates SQL; this module checks it, deterministically — the same
"LLM proposes, code verifies" split Sentinel's z-test and Scribe's
idempotency checks already use.
"""

import contextlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@contextlib.contextmanager
def scratch_copy(source_db_path: Path) -> Iterator[Path]:
    """Copy `source_db_path` to a throwaway temp file, yield its path,
    delete the whole temp directory afterward regardless of success or
    failure (try/finally, not just a happy-path cleanup)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="guardian-remediator-"))
    scratch_path = tmp_dir / "scratch.db"
    shutil.copy2(source_db_path, scratch_path)
    try:
        yield scratch_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dataclass
class ValidationResult:
    """The deterministic verdict on one fix attempt.

    `conserves_rows`: `clean_count + quarantine_count == original_count`
    (checked pre-fix, on `original_table`, before `fix_sql` drops and
    rebuilds it) — catches silent row loss, not just billing_amount
    violations. This is the check the repo owner specifically flagged as
    "strong": a fix that quarantines correctly but accidentally drops rows
    elsewhere would pass a naive violation-count-only check and still be
    wrong.
    """

    success: bool
    original_count: int
    clean_count: Optional[int] = None
    quarantine_count: Optional[int] = None
    violation_count_in_clean: Optional[int] = None
    error: Optional[str] = None  # SQL execution error text, if fix_sql itself failed to run

    @property
    def conserves_rows(self) -> bool:
        if self.clean_count is None or self.quarantine_count is None:
            return False
        return (self.clean_count + self.quarantine_count) == self.original_count


def apply_and_validate_fix(
    scratch_db_path: Path,
    fix_sql: str,
    *,
    original_table: str,
    clean_table: str,
    quarantine_table: str,
    check_column: str = "billing_amount",
) -> ValidationResult:
    """Run `fix_sql` (expected to DROP + rebuild `clean_table` — usually
    the same name as `original_table` — and create `quarantine_table`,
    splitting rows by `check_column >= 0`) against the scratch copy, then
    check deterministically:

      1. Zero rows in `clean_table` violate `check_column >= 0`.
      2. `clean_table` + `quarantine_table` row counts sum to
         `original_table`'s row count as it stood BEFORE `fix_sql` ran
         (read first, held onto, since `fix_sql` itself replaces
         `original_table`'s content).

    `check_column` is compared via `CAST(... AS REAL)` — real, because
    `billing_amount` is TEXT in some tables (`staging_patients`) and REAL
    in others (`claims`); a bare `< 0` on a TEXT column would do a string
    comparison, not a numeric one, and silently pass rows it shouldn't.

    A `sqlite3.Error` while running `fix_sql` (malformed SQL, wrong table/
    column names, etc.) is caught and returned as `ValidationResult(success=False, error=...)`,
    not raised — this is exactly the failure mode Remediator's retry loop
    needs to see and feed back to the model, not crash on.
    """
    conn = sqlite3.connect(str(scratch_db_path))
    try:
        original_count = conn.execute(f"SELECT COUNT(*) FROM {original_table}").fetchone()[0]

        try:
            conn.executescript(fix_sql)
        except sqlite3.Error as e:
            return ValidationResult(success=False, original_count=original_count, error=str(e))

        try:
            clean_count = conn.execute(f"SELECT COUNT(*) FROM {clean_table}").fetchone()[0]
            quarantine_count = conn.execute(f"SELECT COUNT(*) FROM {quarantine_table}").fetchone()[0]
            violation_count = conn.execute(
                f"SELECT COUNT(*) FROM {clean_table} WHERE CAST({check_column} AS REAL) < 0"
            ).fetchone()[0]
        except sqlite3.Error as e:
            # fix_sql ran without error but didn't actually produce the
            # expected clean/quarantine tables (e.g. wrong names) -- a
            # real, distinct failure mode from a syntax error, still
            # handled the same way: reported, not raised.
            return ValidationResult(success=False, original_count=original_count, error=str(e))

        success = (violation_count == 0) and (clean_count + quarantine_count == original_count)
        return ValidationResult(
            success=success,
            original_count=original_count,
            clean_count=clean_count,
            quarantine_count=quarantine_count,
            violation_count_in_clean=violation_count,
        )
    finally:
        conn.close()
