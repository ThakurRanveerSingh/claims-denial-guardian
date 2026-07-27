# Sprint 3, WP3 — The Reporter (audit report, and two rounds of real UAT)

Built against `docs/decisions/0009-reporter-design.md`, implementing US-5
— the last stage of `hld.md`'s pipeline. Four parts: **A** (design sketch,
approved with two changes — a real `generated_at` timestamp instead of
omitting it, and re-running Scribe through the real pipeline instead of
backfilling by hand), **B** (implementation), **C** (proof against both
real incidents, followed by two full rounds of hands-on UAT that each
found real, fixable problems), **D** (this walkthrough). Done entirely in
the main session, no subagent delegation.

## What was built

| Part | Files | Purpose |
|---|---|---|
| A | (in-chat design sketch) | Report structure, templating choice, severity bucketing, rich-terminal scope |
| B | `src/agents/reporter.py`, `src/agents/templates/*.tmpl`, `tests/test_reporter.py` | `audit_report.md`/`audit_report.html` generation — zero LLM, two live db queries for independently-verifiable numbers |
| B | `src/agents/rich_output.py`, `tests/test_rich_output.py` | Optional rich-terminal polish, verified both installed and uninstalled |
| B | `src/agents/orchestrator.py`, `src/agents/cli.py` | Reporter wired as the final pipeline stage (`generate_reports=True` by default, no CLI flag — no side effect to gate) |
| B (unplanned) | `orchestrator.load_incident()`/`resume_incident()`, `guardian resume` CLI subcommand | A real, officially-supported "resume a saved incident from a given stage" capability — needed to backfill the two canonical incidents' `remediator`/`scribe` fields from the real pipeline, not a throwaway script |
| C | Two real PR-adjacent artifacts: `examples/<incident-id>/report/` for both incidents | Live proof, plus two rounds of real UAT |

Test suite: **334 tests total**, 13 more skip cleanly when the optional
`rich` extra isn't installed, 4 marked `@pytest.mark.live`.

## The unplanned detour: `guardian resume`

Part A's approved design required Scribe's real writeback to appear in
both incidents' `incident.json`. The obvious approach — run `guardian run
--remediate` again — turned out to be a trap: every `guardian run` call
mints a fresh, timestamped `incident_id`, and Remediator's PR branch name
is derived directly from it. Re-running the full pipeline can never
backfill an *existing* incident's fields; it can only open a sibling PR
under a new ID. Caught before running it for real, not after — flagged the
conflict, got direction to build a proper fix instead.

Built `load_incident()`/`resume_incident()` in `orchestrator.py` and a
`guardian resume <incident_id> --stage remediate|writeback` CLI
subcommand: reloads the exact saved `Incident` (same `incident_id`, same
`SentinelFinding`/`InvestigatorFinding`) and runs only the requested
downstream stage against it. Verified the idempotency mechanism worked
through this NEW path — not just inferred from a clean exit — with a
`_PoisonedBackend` whose `.complete()` raises if ever called: the backfill
run completed successfully, proving the existing-PR short-circuit fired
before any generation was attempted, not merely that the run happened not
to error.

## Part C, round one: real UAT catches a genuine squint-test failure

Generated both reports, opened both HTML files for real (`open` against
the literal `file://` path, no server), then read both cold as a
non-technical compliance reviewer — not a code review. Two real findings:

1. **Raw agent telemetry in the compliance-facing sections.** "What was
   detected" ended with `see two_proportion_z_test() in
   src/agents/sentinel.py for the exact formula` — a Python function name
   and source file path, directly in prose meant for a non-engineer. The
   Evidence table was worse: literal SQL, `mcp__datahub__get_lineage`
   tool-call names, `Bash (sqlite3 .schema)`. A compliance officer cannot
   follow this without an engineer translating it live.
2. **The lineage diagram's cross-incident contrast was real but not
   instant.** Both diagrams are red-dominated (3-4 of 5 boxes); the
   distinguishing green boxes in UHC's diagram are correctly colored but
   not the first thing that registers at a genuine half-second glance.

Fixed by moving the raw trace to a clearly labeled Technical Appendix
(kept complete, not deleted) and rewriting the narrative sections in plain
English (`_plain_root_cause()`, `_plain_classification()` translate the
coded tags), and by adding a one-line "N of 5 pipeline stages implicated"
caption above each diagram, computed from the same data that colors it.
Both fixes got permanent regression tests, not just fixes — checking the
four leaked patterns are genuinely absent from sections 1-3 (present in
the appendix) for both real incidents, and that the caption states the
correct count.

## Part C, round two: a second real UAT catch, inside the re-check itself

Re-verification isn't a formality here — doing it properly caught a THIRD
real bug the first fix round didn't touch. Checking whether "Actions
Taken" showed real DataHub writeback (not placeholder text) surfaced that
both reports said "tag applied, documentation note added" *uniformly* for
every entity — even though the live terminal output from the Scribe
backfill run (captured minutes earlier) showed a genuine, meaningful mix:
some entities already had the tag from the earlier, disconnected UHC live
test, some didn't. `_actions_taken_lines()` was collapsing Scribe's own
three-way signal (`tag_applied`/`tag_already_present`/neither) into one
word, discarding information `ScribeEntityResult` already carried and
`orchestrator.print_incident_summary()` already surfaced correctly
elsewhere. Fixed to the same three-way wording, with tests covering the
exact mixed case (one entity already-present, one freshly applied) plus a
direct check against the real Cigna file.

Also checked and passed, both rounds: the self-contained/offline claim
(grepped for any `<link>`/`<script src>`/CDN reference — zero; launched
via real `file://`, no server), a hand-verified z-score recomputation from
the report's own stated raw counts (independently recomputed 35.5332
against the report's stated 35.53), and a scan of every evidence entry for
patient-name-shaped text or PII (none found — only table names, claim
IDs, and aggregate counts).

## The lesson, twice over

The same shape both times: the agent answered the question it was asked
("does the data look right?"), not the question that actually matters
("would a non-technical reviewer follow this, and does this report tell
the truth about what actually happened?"). Neither gap was caught by the
test suite passing — both were caught by reading the actual output as the
document's real intended audience would, and in round two, by checking the
report's own claim against a terminal transcript from minutes earlier
rather than trusting that the code must be doing what its docstring says.
Every finding from both rounds left behind a regression test — the
explicit standing instruction for this project going forward: a UAT catch
that doesn't leave a test behind can silently return.
