# Sprint 4, WP1 — Fresh-clone judge simulation

Four parts: simulate a judge with no assumed context (Part A), compile a
severity-ranked punch list without fixing anything (Part B, explicit stop
for triage), fix everything BLOCKING/CONFUSING (Part C), then re-run the
*entire* fresh-clone test from a clean pull of `origin` to prove the fixes
actually close the loop rather than just looking fixed by inspection
(Part D). The last part caught a real bug in one of the Part C fixes
itself — see below.

## Part A: what a judge actually hits

In `/tmp/judge-clone-test`, a real `git clone` from `origin` (not a local
copy), then every command a stranger would run, exactly as written,
nothing silently patched. Findings, in order of how a judge would hit
them:

1. **No root `README.md` anywhere.** Only `src/datahub/README.md`,
   scoped entirely to the DataHub dataset — no install steps, no
   `.env`, no `guardian` CLI mentioned at all. GitHub's landing page for
   this repo rendered a bare file listing.
2. **`guardian run --dry-run` works instantly, zero config.** Sentinel-only,
   no DataHub, no LLM — a genuine positive.
3. **A real `guardian run` with zero `.env` silently degraded.**
   Investigator's own generated evidence said the DataHub MCP server
   "never exposed any tools despite 8 separate ToolSearch attempts... 
   treated as unavailable" and fell back to raw `sqlite_master`
   introspection — it still reached the right answer, but only because
   Sonnet improvised a workaround nothing in the codebase actually
   implements or tests.
4. **Scribe's writeback succeeded anyway, with no token** — contradicting
   `src/datahub/README.md`'s explicit "anonymous requests get a 401."
5. **Zero documentation for standing up DataHub itself.** No
   `docker-compose.yml`, no quickstart command — one line in
   `docs/architecture/hld.md` §2.2 ("Confirmed running locally").
6. **PR #1/#2 on `denial-guardian-data-platform`**: both open, correct,
   well-documented — positive.
7. **`audit_report.html`**: fully self-contained (inline `<style>`,
   system fonts, no external CSS/JS), renders fine fully offline —
   positive.
8. **`pytest tests/`**: 384/384 non-skipped green out of the box — positive
   (13 skips, all the optional `rich` extra, self-documenting).
9. **`guardian check-drift`/`guardian export-fhir`** both fully succeeded
   once a real `.env` was configured — proving the underlying DataHub MCP
   integration itself was sound; #3's failure was specific to how
   Investigator's `claude -p` subprocess resolved its `--mcp-config`.

## Part B: the punch list

Compiled BLOCKING / CONFUSING / COSMETIC (none found severe enough to be
COSMETIC-only), each with exact repro and exact proposed fix, reported to
the repo owner, and — per explicit instruction — **nothing fixed yet**.
Stopped there for triage. Approved unchanged: "go ahead and fix all of
them."

## Part C: the fixes

**BLOCKING #1/#2 — root `README.md`.** Pitch, prerequisites, `pip install
-e .` (mentions the `[rich]` extra), the DataHub bring-up sequence that
never existed anywhere before (`datahub docker quickstart` → ingest →
`add_lineage.py`/`add_metadata.py`/`register_ml_model.py` → generate a
token), a real `.env` template covering every env var the codebase
actually reads, and the command walkthrough. Wired into `pyproject.toml`'s
`readme` field (previously absent).

**BLOCKING #3 — root-caused, not just documented.** `subprocess.run()` in
`llm_backend.py` had no explicit `env=`, so the `claude -p` subprocess
inherited whatever this process's own `os.environ` happened to contain —
with no `.env`, that's nothing at all for `DATAHUB_GMS_URL`/
`DATAHUB_GMS_TOKEN`. `investigator_mcp_config.json`'s `${DATAHUB_GMS_URL}`
expansion then resolved to an empty string rather than any default — a
key that's *present but empty* never triggers `mcp-server-datahub`'s own
`os.environ.get(key, "http://localhost:8080")`-style fallback (that only
fires for a *missing* key). Result: total, silent tool unavailability.
Design A's inline env dict (`investigator.py`, used by the `anthropic`/
`ollama` backends) had the exact same class of bug — defaulted to `""`
instead of a real URL.

Fixed with a shared `_datahub_mcp_env()` helper (the same
`os.environ.get(key, real_default)` convention scribe.py/drift.py/
fhir_export.py already use), a new `env=` parameter threaded through
`LLMBackend.investigate()` → `ClaudeCodeBackend.investigate()` →
`_run_claude()` → `subprocess.run()`, and Design B's call site now passes
`_datahub_mcp_env()` explicitly rather than trusting the `claude` CLI's
own substitution against a possibly-empty environment. 7 new regression
tests (`test_llm_backend.py`, `test_investigator.py`).

**CONFUSING #4 — subsumed** by the README existing at all.

**CONFUSING #5 — progress feedback, and the real cause of "guardian run
looks hung."** A new `on_investigating` callback on `run_guardian()`
(fired right before Investigator starts, orchestrator.py stays print-free
otherwise — that's `cli.py`'s job) prints a line before the long silent
stretch. But the *actual* fix was upstream of that: `mcp-server-datahub`
phones home to `track.datahubproject.io` on essentially every call via
`acryl-datahub`'s own telemetry module, and on a network that can't reach
it, each call burns ~40s in connection-timeout retries — the real reason
every live command in this project (and this whole multi-session
development effort) had been so slow. `DATAHUB_TELEMETRY_ENABLED=false`
is DataHub's own documented opt-out (`get_boolean_env_variable`, any
value but `"true"`/`"1"` disables it) — set via `os.environ.setdefault()`
in every module that touches DataHub (scribe/drift/fhir_export/
investigator), explicit in every `StdioServerParameters`/`--mcp-config`
env dict, and in the repo-root `.mcp.json` too.

**CONFUSING #6 — checked directly, not assumed.** A bare `curl` GraphQL
read with no `Authorization` header, and a real `DatahubRestEmitter(...,
token=None)` write (a scratch tag, created then deleted), both succeeded
against the real running instance. `src/datahub/README.md`'s "Authentication"
section now states the verified reality — this is a real
`datahub docker quickstart` deployment, whose default policies apparently
grant broad access regardless of `METADATA_SERVICE_AUTH_ENABLED` — while
still recommending a token (audit-trail identity; a differently-configured
instance may enforce it strictly).

**CONFUSING #7 — subsumed** by the README's install section mentioning
`pip install -e ".[rich]"`.

## Part D: proving the loop actually closed

Pushed Part C to `origin`, then a genuinely fresh `git clone` into a new
directory (`/tmp/judge-retest`) — not a reused checkout. Re-ran the exact
original failure case first: `guardian run --segment "Cigna,obesity"` with
**zero `.env`**, the precise repro of BLOCKING #3.

**Result: fixed on both axes.**
- Investigator's own `lineage_path_walked` now shows real
  `get_lineage`/`get_lineage_paths_between` tool output — genuine DataHub
  MCP usage, not the raw-SQL fallback.
- Zero `track.datahubproject.io` retry warnings anywhere in the output
  (previously dozens).
- **Wall clock: 158.5s, down from ~879s (14:39) for the equivalent
  pre-fix run** — roughly a 5.5x improvement, entirely from the telemetry
  fix.

**A real bug caught live, in one of the Part C fixes itself**: watching
this re-run for the progress line, it never appeared. The line's own
`print()` call had no `flush=True` — Python block-buffers stdout whenever
it isn't a real terminal (redirected to a file, or captured the way this
project's own tooling does), so the fix for "looks hung" was itself
invisible until the process exited. Fixed immediately (`flush=True`),
tests re-run (408 still green), committed and pushed separately, then
verified again with a `git pull` into the same clone plus a fresh
`guardian run` — the line now appears within ~4 seconds, in real time, as
intended. The same "verify against the live system, don't just trust a
fix looks right" discipline this project has applied at every prior
sprint boundary caught a bug in a fix for exactly that discipline.

With a real `.env` configured (copied token, per the new README):
`guardian run --segment "UnitedHealthcare,diabetes"` — 231.9s, real
lineage tool usage again. `guardian check-drift` — **3.4s total**
(`user 1.68s system 0.52s cpu 64% total 3.392s`), down from ~8+ minutes.
`guardian export-fhir` — **1.8s total**, down from several minutes. PR
#1/#2 reconfirmed open and correct. The regenerated `audit_report.html`
reconfirmed self-contained (one inline `<style>` block, one plain
clickable link, zero external CSS/JS references).

## Numbers, for the record

Direct before/after (same segment, same zero-`.env` scenario — the only
apples-to-apples comparison Part A actually produced):

| Command | Before | After |
|---|---|---|
| `guardian run` (Cigna/obesity, zero `.env`) | ~879s (14:39) | 158.5s |
| DataHub MCP telemetry retries observed | dozens | 0 |

Part A never ran `guardian run` against UnitedHealthcare/diabetes or
timed `check-drift`/`export-fhir` in a zero-`.env` state, so there's no
valid pre-fix number for those specifically — only the post-fix result,
reported here without a fabricated baseline:

| Command | Post-fix (real `.env`) |
|---|---|
| `guardian run` (UnitedHealthcare/diabetes) | 231.9s |
| `guardian check-drift` | 3.4s total (`1.68s user, 0.52s system, 64% cpu`) |
| `guardian export-fhir` | 1.8s total |

For qualitative context: Part A's original `check-drift`/`export-fhir`
runs (against Cigna/obesity, with a real token, telemetry still enabled)
each took several minutes — the 3.4s/1.8s figures above make the scale of
the telemetry fix's impact clear even without a segment-matched baseline.

## Test suite

397 → 408 tests (11 new: `_datahub_mcp_env()` real-default behavior,
`env=` passthrough on `ClaudeCodeBackend.investigate()`, Design B actually
passing it, `on_investigating` firing correctly and defaulting to silent,
`DATAHUB_TELEMETRY_ENABLED` present in `drift.py`/`fhir_export.py`'s
`_server_params()`). All green, no live calls added.
