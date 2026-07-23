# 0002 — Copy the healthcare fixture into `src/datahub/` instead of using it in place

Date: 2026-07-23
Status: Accepted

## Context

The `healthcare.db` SQLite database (and the scripts that build/describe it —
`create_db.py`, `ingest.yaml`, `add_lineage.py`, `add_metadata.py`, `README.md`)
lives at `~/static-assets/datasets/healthcare/`, outside this repo. It's a
general-purpose teaching fixture (Kaggle "Healthcare Dataset", CC0, seeded with
`random.seed(42)` for reproducibility) that other exercises may also use in its
pristine form — it isn't specific to Claims Denial Guardian.

Sprint 1 needs to *extend* this database: add `claims`, `denials`, and
`denial_model_scores` tables (see `docs/architecture/lld-sprint1.md` §1),
generate synthetic denial data into it, and re-run its ingestion/lineage
scripts against the extended schema. That means mutating the database file
in place.

## Decision

Copy the entire folder's contents into `src/datahub/` in this repo, and do
all Sprint 1 (and later) work against that copy. The original at
`~/static-assets/datasets/healthcare/` is left untouched.

## Alternatives considered

- **Work against the file in place at `~/static-assets/...`.** Rejected —
  the hackathon submission is judged by cloning this repo; a dependency on
  a file outside the repo means judges can't reproduce the demo. It also
  risks corrupting a shared fixture other exercises might expect pristine.
- **Copy only `healthcare.db`, leave the scripts referenced from the shared
  location.** Rejected — `add_lineage.py` and `add_metadata.py` hardcode
  `PLATFORM = "sqlite"` and `DEFAULT_INSTANCE = "healthcare"`, tightly
  coupled to this exact database. Keeping the scripts and the data they
  describe together, and extending both in place, avoids two lineage
  scripts quietly drifting apart from each other.
- **Symlink instead of copy.** Rejected — a symlink to a path under the
  user's home directory (`~/static-assets/...`) still isn't reproducible
  for anyone else who clones the repo; it would resolve to nothing on any
  other machine.

## Consequences

- `src/datahub/` is now self-contained: anyone who clones the repo gets the
  exact same starting database and scripts, no external path dependency.
- `healthcare.db` is a ~31 MB binary file. Committing it (and its future
  mutations, as Sprint 1 adds tables and seeds data into it) grows the git
  history by roughly that much per change. Considered gitignoring it and
  regenerating via `create_db.py`'s fixed seed instead, and Git LFS —
  decided against both: this is a hackathon-timeline project that won't be
  cloned/rebuilt often, so the extra setup step (regeneration) or extra
  dependency (LFS) isn't worth it against the simplicity of just committing
  the file like any other. **Decision: commit `healthcare.db` as a normal
  tracked file, no `.gitignore` entry, no LFS.**
- The original fixture at `~/static-assets/datasets/healthcare/` stays
  untouched, so other exercises relying on the pristine version are
  unaffected.
