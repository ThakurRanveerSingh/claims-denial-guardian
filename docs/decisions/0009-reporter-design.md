# 0009 — Reporter's design: audit report, technical appendix, rich terminal

Date: 2026-07-27
Status: Accepted

## Context

Sprint 3 WP3 builds Reporter (US-5): given a completed `Incident`, generate
a compliance-officer-readable Markdown document and a self-contained HTML
page, plus optional rich-terminal polish for `guardian run`. This is the
last stage of `hld.md`'s pipeline to be built — every earlier stage
(Sentinel, Investigator, Scribe, Remediator) now has a downstream consumer
that turns its findings into something a human outside engineering can
actually read.

## Decision

### 1. Zero LLM calls, two live db queries for independent verifiability

Same "LLM proposes, code verifies" boundary this project keeps everywhere
else — except here there's no LLM proposing anything at all; Reporter only
renders decisions earlier stages already made. The one place it does real
work at generation time: member-impact counts and the four raw counts
behind the z-test (`load_member_impact`, `load_baseline_context`) are
re-derived fresh from a read-only connection to the real `healthcare.db`,
not merely echoed from `Incident`'s own cached JSON fields. This is what
makes the audit document's "recomputable" claim actually true — a reader
can independently verify the numbers against the database itself, not just
trust that Sentinel's/Investigator's already-summarized figures are still
accurate.

### 2. Templating: stdlib `string.Template` + `html.escape()`, not Jinja2

Jinja2's autoescaping and native `{% for %}` loops are real, honest
advantages for the HTML side. Rejected anyway, to stay consistent with how
every other deterministic template in this codebase (Scribe's doc-note
text, Remediator's PR body) is built — the actual templating need here (a
fixed handful of sections, bounded list iteration for a handful of tables)
doesn't need a real templating engine's loop/inheritance machinery to stay
correct and simple. A second dependency for a problem the stdlib already
handles cleanly is the wrong trade for this project's stated minimalism.

### 3. `generated_at` is a real timestamp; determinism is proven by
   normalizing it, not omitting it

The alternative — leave it out of the content entirely to make byte-
identity trivial — was explicitly rejected: real information belongs in
the report. The golden-file test (`tests/test_reporter.py`) generates each
format twice against the real saved incidents and normalizes the one
named, explicitly-documented field (`generated_at`) before diffing, then
asserts every OTHER line is untouched. This is a stronger proof than a
naive full-file diff would be: it demonstrates specifically which field
varies and that nothing else does, not just that two outputs happen to
match.

### 4. Technical Appendix, separated from the compliance-facing narrative

**This decision exists because of a real UAT failure, not upfront design.**
The first version of this module put raw agent telemetry — Python function
names, source file paths, literal SQL, `mcp__datahub__*` tool-call syntax,
and coded tags like `introduced_at:claims` — directly into the sections a
compliance officer is meant to read standalone. The repo owner's own
cold read of the Markdown file (not a code review) caught this directly:
"a non-technical compliance reviewer cannot follow this without an
engineer translating it live."

Fixed by keeping the full raw trace — genuinely valuable for
reproducibility and engineering review — but moving it to a clearly
labeled "Technical Appendix" section at the end of both formats (in HTML:
visually distinct via a dashed border and muted panel), and rewriting the
narrative sections (`_detection_narrative`, `_investigation_narrative`,
the root-cause breakdown table, the origin-split legend) to use plain
English throughout: `_plain_root_cause()` translates `introduced_at:X` /
`inherited_from:X` into full sentences, `_plain_classification()` strips
the coded prefix from breakdown entries. `root_cause_summary` itself is
kept verbatim — it's Investigator's own already-narrative finding, and
rewriting it would mean editorializing another agent's conclusion, the
same discipline Remediator's PR body already applies to this exact field.

A permanent regression test (`test_md_and_html_have_no_function_names_
file_paths_or_raw_lineage_syntax_outside_appendix`) asserts the four
leaked patterns are absent from sections 1-3 and present in the appendix,
for both real incidents — turning a one-time human catch into a guard
against this exact bug class returning.

### 5. Lineage diagram uses a fixed, known pipeline topology — not
   `lineage_path_walked`

`InvestigatorFinding.lineage_path_walked` is free-text narration of the
TOOL CALLS Investigator made (e.g. `"get_lineage(upstream, urn=claims,
max_hops=3) -> mart_billing (degree 1), ..."`), not a clean list of bare
node names — confirmed directly against both real saved incidents, not
assumed. A diagram built by membership-testing that narration against
`affected_branch`/`datasets_checked_and_clean` (both bare table names)
would never match and color every node the same gray. `PIPELINE_TOPOLOGY`
— the same five real tables, in the same fixed order
`fresh_build_validation.py`'s own `TRANSFORM_ORDER` already encodes, with
`raw_patients` prepended — is used instead: a genuinely simple,
correct, "left-to-right" diagram, matching Part A's explicit ask for
"simple," not an exact dependency graph (`mart_billing`/`mart_demographics`
are really a parallel fork off `staging_patients`, flattened into one row
on purpose).

### 6. Lineage diagram caption: "N of 5 pipeline stages implicated"

A second, real UAT finding: both real incidents' diagrams are red-dominated
(3-4 of 5 boxes), so the distinguishing signal between two incidents read
side by side — UHC's two green boxes — is real but not the FIRST thing
that registers at a genuine half-second glance. Added a one-line caption
above the diagram, computed from the exact same red/green node
classification already used to render it (not a second, independently
derived count that could drift out of sync). A reader now registers "2"
vs "4" as a number before parsing which boxes differ — verified as the
right fix by the repo owner's own re-check: "with the caption anchoring
it, does the pair read as obviously different now?"

### 7. `rich` is optional, not a core dependency

`src/agents/rich_output.py` guards its own `import rich` in a try/except
and falls back to `orchestrator.py`'s existing plain-text
`print_incident_summary()`/`print_dry_run_summary()`. Listed as an extra
(`pip install -e ".[rich]"`), not in `[project]`'s own `dependencies` —
a hard dependency would mean the plain-text branch never actually runs
for anyone with a normal install, making "plain-text fallback if rich
absent" untested in practice. Verified in both directions this session:
real ANSI color codes confirmed present with `rich` installed, full test
suite re-run clean with `rich` uninstalled to prove the fallback isn't
just theoretical.

### 8. No new CLI flag for report generation

Unlike `--remediate`/`--no-writeback`, report generation has no external
side effect — a pure read (the real `healthcare.db`, already-in-memory
`Incident` fields) followed by a local file write. No DataHub write, no
PR, nothing to consent to. `generate_reports` exists as a keyword
parameter on `run_guardian()` (default `True`) purely as a programmatic
escape hatch for tests exercising synthetic segment names Reporter's own
live db queries can't resolve — not exposed via the CLI.

### 9. Actions Taken wording distinguishes "already present" from
   "freshly applied" — a second real UAT catch

An earlier version of `_actions_taken_lines()` collapsed Scribe's own
three-way idempotency signal (`tag_applied` / `tag_already_present` /
neither) into one word ("applied"/"not applied"), discarding information
`ScribeEntityResult` actually carries and `orchestrator.print_incident_
summary()` already surfaces correctly elsewhere. Caught by checking the
report's own text against what a live `resume_incident(stage="writeback")`
run had just printed to the terminal for the same incident — not by
re-reading the code, the same "verify against the live system" method
this project has used throughout. Fixed to the same three-way wording,
with regression tests covering the mixed case directly (one entity
already-present, one freshly applied — the real shape the Cigna
incident's own first backfill run produced).

## Alternatives considered

- **Jinja2 for HTML.** Rejected — section 2.
- **Omit `generated_at` for trivial byte-identity.** Rejected — section 3.
- **Delete the raw evidence trace entirely for compliance readability.**
  Rejected — reproducibility matters; the fix was relabeling and moving
  it, not deleting it (section 4).
- **Drive the lineage diagram from `lineage_path_walked` directly.**
  Rejected once its real shape (tool-call narration, not node names) was
  checked directly — section 5.
- **`rich` as a hard dependency.** Rejected — section 7.
- **A `--no-reports` CLI flag for symmetry with `--no-writeback`.**
  Rejected — there's no side effect to opt out of; symmetry with a
  different kind of stage isn't a reason to add a flag nothing needs
  (section 8).

## Consequences

- `src/agents/templates/audit_report.{md,html}.tmpl` are real, versioned
  files, not inline strings — template changes are visible in diffs
  independent of the Python that fills them in.
- `Incident.pipeline_stages_run` gains `"report"` when generation runs, the
  same extension pattern `"scribe"`/`"remediator"` already established.
- Two real UAT findings (sections 4/6, and 9) each left behind a
  permanent regression test, not just a fix — the repo owner's explicit
  standing instruction for this project: "every UAT finding should leave
  behind a test, not just a fix."
