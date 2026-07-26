# 0007 — Scribe's DataHub writeback design

Date: 2026-07-26
Status: Accepted

## Context

Sprint 3 WP1 builds Scribe (US-4): given an `Incident` with a confident or
inconclusive `InvestigatorFinding`, write what was learned back into
DataHub — an incident tag, a documentation note on the implicated entities,
and a structured record of the violated expectation. `hld.md`'s original
four-stage pipeline (Sentinel → Investigator → Remediator → Scribe) named
this stage but never designed it; `lld-sprint2.md` §4.1 explicitly built
`Incident`'s shape so Scribe (and Remediator) could "append their own
sections to an existing record later... without this contract needing to
change shape" — this is that moment.

Per decision 0003: reads go through the DataHub MCP server, writes go
through the `acryl-datahub` SDK's REST emitter with `DATAHUB_GMS_TOKEN`.
Scribe follows this split exactly.

## Decision

### 1. Incident tag: one generic `urn:li:tag:guardian-incident`, not one per incident

Matches this project's existing tag taxonomy (`pii`, `critical`, `internal`,
`quality_monitored`, `pipeline_stage` — a small, stable, reusable set, not
one-off entities). A tag-per-incident would permanently pollute the tag
namespace and break "browse by category." Per-incident specificity (which
incident, when, root cause) lives in the documentation note instead.
Idempotent via set-union: read the entity's current tags (via MCP), union in
`guardian-incident`, write back only if not already present.

### 2. Documentation append, `affected_branch` only, via `institutionalMemory`

Never `datasetProperties.description` (a single overwritable string already
used by `add_metadata.py`) — `institutionalMemory` is DataHub's actual
append mechanism (the Documentation/Links tab: a list of
`{url, description, createStamp}`). **This is a whole-list aspect** —
confirmed live that `claims` currently has `institutionalMemory: null`, but
since the aspect is replaced wholesale on every write (the same class of
risk diagnosed in Part A for `datasetProperties`/`register_ml_model.py`),
Scribe must read the existing list before writing, every time, not just on
a theoretical future incident. Idempotency: each entry's description embeds
the `incident_id`; before appending, check whether an entry for that
`incident_id` already exists and skip/replace rather than duplicate.

Only entities named in `InvestigatorFinding.affected_branch` get a tag or a
doc note — never `datasets_checked_and_clean`. This maps directly onto
US-2's "selective halting" story: Investigator already decided which
entities are actually implicated: Scribe does not re-decide this, it
executes exactly what Investigator concluded, on the entities Investigator named.

**`url` field**: a GitHub blob link to the incident's own committed
`examples/<incident_id>/incident.json`, constructed from the repo's actual
`git remote get-url origin` output (parsed into `https://github.com/<org>/
<repo>/blob/main/<path>`), never hardcoded. This assumes the incident file
has been pushed by the time anyone clicks the link — a real, stated
assumption, not a guarantee. Flagged for the Sprint 4 pre-demo checklist:
push before filming, so the links actually resolve live.

### 3. Assertion — verified live, not assumed from SDK class presence

Per the MLModel-lineage lesson (Sprint 1: an entity the SDK models but the
live GMS server-side rejects for one specific relationship), every claim
below was checked against the real local GMS, not inferred from the SDK
having the right classes:

- Emitted a real `AssertionInfoClass` (`type=DATASET`,
  `DatasetAssertionInfoClass(scope=DATASET_COLUMN,
  operator=GREATER_THAN_OR_EQUAL_TO, fields=[<schemaField urn>],
  parameters={value:"0"})`) against `claims` — **accepted, persisted, and
  correctly appears on `claims`'s own `assertions` relationship** (confirmed
  via a live GraphQL read-back, not just "no exception raised"). Unlike the
  MLModel→Dataset lineage edge, this is real support, not a dead end.
- **A real, fixable gotcha found along the way**: the `schemaField` URN
  needs the dataset's *full* URN as its parent, not the bare table name.
  First attempt (`urn:li:schemaField:(claims,billing_amount)`) was rejected
  by the live GMS with:
  ```
  Invalid urn: urn:li:schemaField:(claims,billing_amount)
   Cause: ERROR :: /parent :: "Provided urn claims" is invalid:
   Urn doesn't start with 'urn:'. Urn: claims at index 0: claims
  ```
  Fixed by using `urn:li:schemaField:(<full dataset urn>,billing_amount)`.
  Re-emitted, accepted.
- **Idempotency of the assertion *definition***: one assertion per
  `(dataset, expectation)` pair, not per incident — e.g.
  `guardian-billing-amount-non-negative-claims`. A stable URN; re-emitting
  identical content is harmless (Scribe checks existence first and skips if
  already defined, rather than relying purely on overwrite-with-identical-
  content).
- **Idempotency of the assertion *run event* — measured, not assumed**: an
  `AssertionRunEventClass` is a timeseries aspect. Emitted the *identical*
  run event twice (same `assertionUrn`, `timestampMillis`, `runId`) and
  confirmed via live read-back that `runEvents.total` stayed at **1**, not
  2 — DataHub's own timeseries store deduplicates on that key tuple.
  Design: derive `runId = incident_id` and `timestampMillis` from
  `incident.created_at`, both deterministically — reruns of the same
  incident produce zero duplicate run events for free, with no
  application-side dedup logic needed for this one specifically.

### Scope: one concrete expectation, not a generic parser over free text

Scribe targets exactly the one expectation both of Sprint 2's real seeded
incidents actually violate — `billing_amount >= 0` — rather than building a
system that parses an arbitrary expectation out of
`InvestigatorFinding.root_cause_summary`'s free-text prose. `InvestigatorFinding`
has no structured `{column, operator, threshold}` field today; only natural-
language text. Building a generic parser now would be speculative
generalization against a single demonstrated violation type — YAGNI applied
correctly: generalize when a second violation type actually exists, not
before. Before asserting on any `affected_branch` entity, Scribe confirms
(via a live MCP schema read, not assumed) that the entity actually has a
`billing_amount` column — an entity without one (e.g. `mart_demographics`,
which has no `billing_amount` column at all) is skipped, not crashed on or
asserted against meaninglessly.

**Designated extension point for the next violation type**: add a
structured `{column, operator, threshold}` field to `InvestigatorFinding`
(populated by Investigator's own hypothesis-testing step, which already
knows exactly which column and comparison it tested) — not a parser over
`root_cause_summary`'s prose. That's the correct place for this to grow.

## Alternatives considered

- **A tag URN per incident.** Rejected — permanent namespace pollution,
  breaks the existing tag-as-category convention; per-incident detail
  belongs in the doc note, not the tag.
- **Overwriting `datasetProperties.description` for the incident note.**
  Rejected — that field is a single string already used for the table's own
  description; overwriting it for incident tracking would destroy existing
  documentation and isn't even appendable.
- **A fully generic assertion system parsing expectations from LLM prose.**
  Rejected this sprint — no second violation type exists yet to generalize
  against; the correct extension point is a structured field on
  `InvestigatorFinding`, not a prose parser, when that day comes.
- **Hard-deleting the leftover test assertion** (`urn:li:assertion:
  guardian-billing-amount-non-negative`, created during this design's live
  verification, before the real per-dataset naming convention was decided).
  Soft-delete was tried first and did not take effect in this DataHub
  version — the `status` aspect read back `null` after the call, and the
  entity kept showing as a live, failing assertion on `claims`'s health
  indicator. Rejected escalating to a hard delete on a live shared instance
  for a low-stakes cleanup; instead relabeled its description to mark it a
  superseded test artifact, logged here. It will keep showing as a distinct
  (relabeled, clearly-marked) assertion in the UI — the real
  `...-non-negative-claims` assertion is a different URN and does not
  supersede it automatically.

## Consequences

- `src/agents/scribe.py` needs its own MCP-session-spawning code
  (duplicating `investigator.py`'s pattern, per this codebase's existing
  "copied rather than shared" convention for this kind of per-script
  boilerplate) — not refactored into a shared module this sprint, since
  that wasn't asked for and touching already-committed, already-live-tested
  `investigator.py` is its own decision, not a side effect of building
  Scribe.
- `Incident` gains a `scribe` field and `"scribe"` joins
  `pipeline_stages_run` — the extension `lld-sprint2.md` §4.1 explicitly
  anticipated.
- The GitHub-blob-URL doc links require the repo to be pushed before they
  resolve — a real, load-bearing pre-demo step, not just documentation.

## Amendment (Part C implementation) — two real bugs the live test caught, not assumed away

Both found by `tests/test_scribe.py`'s live test — the exact reason that
test exists — not discovered by inspection:

1. **The MCP `get_entities` tool response has no `"result"` wrapper.**
   Part B's own exploration used Claude Code's *loaded* `mcp__datahub__get_entities`
   tool, which presented the response as `{"result": {...}}`. Calling the
   identical underlying MCP tool through the raw `mcp` Python SDK's
   `session.call_tool()` — what `scribe.py` actually uses at runtime — returns
   the entity's fields at the top level, no wrapper. The wrapper was
   something Claude Code's own tool-presentation layer added, not part of
   the wire response. This silently made `_has_billing_amount_column`
   receive `{}` and return `False` for every entity, including `claims` —
   caught because the live test asserted `assertion_run_event_emitted` was
   `True` and it wasn't.
2. **`relatedDocuments` (from `get_entities`) is not `institutionalMemory`.**
   Part B's exploration saw a `relatedDocuments: {start, count, total}`
   field on a `get_entities` response and reasonably assumed — given the
   shape's resemblance to DataHub's other paginated relationship fields —
   that it was this MCP server's name for `institutionalMemory`. It reads
   back `total: 0` immediately after a real `institutionalMemory` write
   that a direct GraphQL query confirmed had landed correctly — it's a
   different, unrelated DataHub feature, and the MCP tool has no parameter
   to request `institutionalMemory` specifically. **Resolution**: added
   `_read_institutional_memory()`, a direct SDK/GraphQL read
   (`DataHubGraph.execute_graphql`) — a real, checked exception to "reads
   go through MCP," permitted by `CLAUDE.md`'s actual rule ("MCP server
   **or** SDK"), not a violation of it. Caught because the live test's
   *second* run (the idempotency check) found the doc note it had just
   written in run one wasn't recognized as already present.

Both are now load-bearing reasons this module has a live test at all —
neither would have been caught by the mocked unit tests alone, since the
mocks were built from the same (wrong) assumptions as the code they were
mocking. Recorded here, and in `scribe.py`'s own module docstring and the
two functions involved, per the repo owner's standing instruction: this is
exactly the kind of hard-won platform detail worth pinning down for the
next person (or the next sprint) rather than letting it be re-discovered.
