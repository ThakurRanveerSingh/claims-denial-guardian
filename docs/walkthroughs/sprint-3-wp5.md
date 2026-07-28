# Sprint 3 stretch, WP5 — FHIR Compliance Bridge

Built against `docs/decisions/0012-fhir-compliance-bridge.md`. A
half-day, hard-capped thin slice, done after Sprint 3's four core work
packages (Scribe/Remediator/Reporter/Drift) already closed: CMS-0057-F
requires FHIR-based prior-auth/claims data exchange, so this extends
Guardian's findings into CMS-mandated FHIR resources — flagging which
ones are built on data currently under active quality investigation.

## What was built

| Part | Files | Purpose |
|---|---|---|
| A | (in-chat design sketch) | Field-by-field real-vs-placeholder accounting, the data-quality extension shape, DataHub reuse plan, explicit out-of-scope list |
| B | `src/agents/fhir_export.py` | `run_fhir_export()` (deterministic EOB templating, zero LLM), `run_fhir_writeback()` (DataHub dataset + lineage + tag/doc-note, reusing Scribe's pattern) |
| B | `src/agents/cli.py` | `guardian export-fhir <incident_id> [--limit N]` |
| B | `tests/test_fhir_export.py` | 22 structural tests + 1 live idempotency test |
| C | Both canonical incidents | Real `guardian export-fhir` runs against the real DataHub, twice, to prove idempotency |
| D | `docs/decisions/0012-fhir-compliance-bridge.md`, this file | Design record and what was actually built |

Test suite: **397 non-live tests green** (23 new), 6 marked
`@pytest.mark.live` (excluded by default).

## Part A: the honesty line, scrutinized before any code

The repo owner's explicit ask at the design STOP: read the "what's
honestly populatable vs. placeholder" list carefully — the line between a
legitimate demo feature and something that could look like overclaiming
regulatory readiness. This project's `claims`/`denials` schema has zero
ICD-10, CPT, CARC, or NPI data anywhere — only free-text fields and an
internal denial-reason enum. Every required FHIR element was checked
individually against what's actually derivable (full table: decision
0012 §2). The one design decision everything else hinges on: `type`, a
required `CodeableConcept` this project has no real data for, is
represented via FHIR's own `data-absent-reason` extension — the
element is structurally present (satisfies cardinality) but carries only
the extension, no fabricated coding. Not a guess, not an omission — the
standard's own mechanism for telling the truth about a genuine gap.

Diagnosis and adjudication-reason fields are `.text`-only throughout, no
`.coding` ever populated. `patient`/`insurer`/`provider` are
display-only references — no fake `.reference` pointing at a
Patient/Organization resource that was never created. The custom
data-quality extension is namespaced under this repo's own GitHub URL
(resolved live from `git remote get-url origin`, never hardcoded),
pointing at the decision doc itself — not a fabricated `/fhir/` path that
would imply a real, hosted, registered profile.

Scope decision: sampling is bounded (3 claims per incident by default,
`--limit` overridable) and filtered to the one denial reason code
(`INVALID_BILLING_AMOUNT`) both canonical incidents' own root-cause
breakdowns already implicate — not a bulk export of the full segment,
and not an arbitrary cross-section that might miss the actual defect.

Design took ~20 minutes against a 15-minute target — spent reading real
schema and data rather than guessing at field mappings. Approved
unchanged, with the time overage flagged honestly rather than rounded
down to "on schedule."

## Part B: implementation, reusing Scribe's writeback pattern directly

`fhir_export.py` is two entry points, same split as Drift: a pure,
zero-LLM `run_fhir_export()` (real claims/denials rows -> deterministic
EOB dicts -> files under `examples/<incident_id>/fhir/`), and
`run_fhir_writeback()`, which extends Scribe's exact
tag/institutionalMemory-doc-note pattern onto one persistent DataHub
dataset entity representing the export artifact type (platform `file` —
an honest label; this is JSON on disk, not a queryable table), plus a
one-time lineage edge to `raw_patients` resolved live via MCP search. No
new writeback mechanism was invented — the same reuse `drift.py` already
demonstrated for the MLModel entity, applied here to a synthetic file
dataset.

23 structural tests were added (22 non-live, 1 live) — deliberately
structural only, per the design's explicit scope cap: valid JSON,
required R4 elements present with the right shape, the
data-absent-reason mechanism exercised correctly, no fabricated coding
anywhere. Real HL7 conformance validation against a Da Vinci profile is
out of scope, stated plainly rather than silently skipped. 397 non-live
tests green after this addition (up from 374).

## Part C: proof, live, against both canonical incidents

`guardian export-fhir` was run for real against both canonical incidents
— not simulated. Cigna/obesity's resources carry
`classification: "inherited_from:raw_patients"`; UnitedHealthcare/
diabetes's carry `classification: "introduced_at:claims"` — the two
distinguishing framings the design called for, confirmed live in the
generated JSON, not just asserted at design time. For both incidents,
the sampled claims' `net`/`total` amounts are genuinely negative — the
same billing-amount defect Guardian's Investigator is reporting on,
carried through unaltered. **This is deliberate, not a bug in the
exporter**: the compliance artifact visibly shows the defect it's
flagging, in situ — the extension mechanism explains why, the raw number
itself is the evidence.

DataHub writeback was verified live, twice: the first incident's export
registered the dataset, resolved `raw_patients` lineage live (confirmed
against the real returned URN, never hardcoded), applied the tag, and
added a doc note. The second incident's export correctly reported `tag
already present` (same shared artifact-type entity) while still adding
its own distinct doc note — proving both the entity-level idempotency
and the incident-level append-only behavior in the same run, not one at
the expense of the other. Re-running the first incident's export a
second time produced byte-identical files (deterministic sampling +
templating) and a writeback that reported both `tag already present` and
`doc note already present` — idempotency proven at the file-generation
layer and the DataHub layer both, not just claimed.

One real-world note from this proof pass: each live `guardian
export-fhir` invocation took several minutes wall-clock, almost entirely
spent in the DataHub MCP subprocess's own telemetry-retry backoff
(repeated `track.datahubproject.io` connection-timeout retries, ~40s per
MCP call) rather than in this module's own code — external latency, not
a defect in the export logic itself, but worth recording honestly rather
than leaving the half-day cap's wall-clock accounting to look better than
it actually was.

## What this is, and isn't (for the submission text)

**Is**: a CMS-0057-F compliance-linkage demonstration — real FHIR R4
`ExplanationOfBenefit` resources, deterministically generated from this
project's real investigated claims data, each carrying a data-quality
extension that traces the resource back to the specific Guardian
incident and root-cause classification implicating the data it's built
on, registered in DataHub with real lineage back to `raw_patients`.

**Isn't**: a production FHIR server or API. No persistence/query layer
beyond flat files, no OAuth/SMART-on-FHIR, no conformance validation
against the Da Vinci PAS/PDex Payer-Network implementation guides CMS-
0057-F actually specifies, no real ICD-10/CPT/CARC coded values anywhere
(diagnosis and adjudication-reason are `.text`-only by design, not by
oversight), and no other FHIR resource types (Patient, Coverage, Claim,
Bundle). A judge who knows FHIR should read this as a deliberately scoped
linkage demo that tells the truth about its own gaps — including via
FHIR's own data-absent-reason mechanism for the one required field this
project's data genuinely can't populate — not as a claim of regulatory
readiness.
