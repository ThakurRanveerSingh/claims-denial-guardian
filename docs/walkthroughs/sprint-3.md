# Sprint 3, WP1 — The Scribe (writeback to DataHub)

Built against `docs/decisions/0007-scribe-writeback-design.md`, implementing
US-4. Worked through five parts, each reviewed before the next began: **A**
(triage a pre-existing test failure), **B** (design sketch, approved with
amendments), **C** (implementation), **D** (live proof + UI click-path),
**E** (this walkthrough). Done in the main session throughout, not
delegated to a subagent — the explicit Sprint 3 process change logged in
`docs/token_ledger.md`'s 2026-07-26 entry, applied for the first time here.

## What was built

| Part | Files | Purpose |
|---|---|---|
| A | `src/datahub/register_ml_model.py`, `src/datahub/README.md` | Fixed a real drifted-state bug: `denial_model_scores`'s `produced_by_model` custom property, wiped by a later `datahub ingest` re-run |
| B | `docs/decisions/0007-scribe-writeback-design.md` | Tag, documentation, and assertion write design — verified live before committing to any of it |
| C | `src/agents/scribe.py`, `tests/test_scribe.py` | The Scribe module: tag + doc note + assertion, only on `affected_branch` entities |
| C | `src/agents/orchestrator.py`, `src/agents/cli.py`, `tests/test_orchestrator.py` | Wired in as the stage after Investigator; `--no-writeback` to skip |
| D | (no new files — live proof + a UI walkthrough) | Ran Scribe against the real saved incident, read everything back |

Test suite: **185 tests total** (32 new in `test_scribe.py`, 4 new in
`test_orchestrator.py` for the writeback wiring), 182 run by default, 3
marked `@pytest.mark.live` (Sentinel/Investigator's from Sprint 2, plus
`test_scribe.py`'s new one).

## Results

**Part A**: root-caused (not guessed) via a live GraphQL query —
`denial_model_scores`'s `customProperties` really was empty. Mechanism:
`datahub ingest`'s sqlalchemy source re-emits the whole `datasetProperties`
aspect on every run, and Sprint 2's Slice 0 re-ran it after the
two-scenario reseed without re-running `register_ml_model.py` afterward.
Fixed by re-running it (verified live: property restored) and documenting
the ordering dependency prominently so it doesn't silently drift again.

**Part C/D — all three writes verified live, twice each** (once via the
automated live test, once by the repo owner clicking through the actual
DataHub UI):

- **Tag**: a single generic `guardian-incident` tag, applied to `claims`
  and `raw_patients` (both in `INC-20260726T023526Z-unitedhealthcare-diabetes`'s
  `affected_branch`) — confirmed absent from `mart_billing`/`staging_patients`
  (`datasets_checked_and_clean`), both via GraphQL read-back and by the
  repo owner opening `mart_billing` directly in the UI.
- **Documentation**: a note on each implicated entity, linking to the
  incident's own `examples/<id>/incident.json` on GitHub, built from the
  real `git remote get-url origin` — not hardcoded.
- **Assertion**: `billing_amount >= 0`, one per `(dataset, expectation)`
  pair (`guardian-billing-amount-non-negative-claims`,
  `...-raw_patients`), each with its own run event recording this
  incident's specific violation count.
- **Idempotency, measured not assumed**: ran Scribe against the same
  incident twice — second run reported `tag_already_present`/
  `doc_note_already_present` for both entities, zero new writes, and the
  assertion run-event count was unchanged (DataHub's own timeseries store
  deduplicates on `(assertionUrn, timestampMillis, runId)`, verified live
  during Part B).

## Design, briefly (full reasoning in decision 0007)

Three writes, `affected_branch` only: a generic incident tag (not
one-per-incident — avoids permanently polluting the tag namespace); a
documentation note via `institutionalMemory` (read-before-write, since it's
a whole-list aspect); an assertion targeting the one concrete,
scope-approved expectation both real seeded incidents actually violate
(`billing_amount >= 0`) rather than a speculative parser over Investigator's
free-text summary. Scope explicitly leaves a designated extension point:
add a structured `{column, operator, threshold}` field to
`InvestigatorFinding` when a second violation type actually exists — not
before.

## Real problems hit and fixed — not a clean run

1. **Part A's drifted-state bug** — see Results above.
2. **The MCP `get_entities` tool response has no `"result"` wrapper.**
   Part B's exploration used Claude Code's own *loaded* tool interface,
   which presented the response as `{"result": {...}}`. Calling the
   identical MCP tool through the raw `mcp` Python SDK's
   `session.call_tool()` — what `scribe.py` actually uses — returns the
   entity's fields at the top level. The wrapper was something Claude
   Code's own tool-presentation layer added, not the real wire response.
   This silently made every entity look like it had no `billing_amount`
   column. Caught by the live test, not by inspection.
3. **`relatedDocuments` (from `get_entities`) is not `institutionalMemory`.**
   A different, unrelated DataHub feature that happens to share the same
   `{start, count, total}` shape — it read back `total: 0` immediately
   after a real `institutionalMemory` write a direct GraphQL query
   confirmed had landed. Fixed with a real, documented exception to "reads
   go through MCP": `institutionalMemory` is read via
   `DataHubGraph.execute_graphql()` directly — `CLAUDE.md`'s actual rule is
   "MCP server **or** SDK," and this is that "or," exercised for a checked
   reason. Caught by the live test's *second* run (the idempotency check
   didn't recognize a doc note it had just written).
4. **The schemaField URN needs the full dataset URN as its parent, not the
   bare table name.** First attempt at defining the assertion
   (`urn:li:schemaField:(claims,billing_amount)`) was rejected by the live
   GMS:
   ```
   Invalid urn: urn:li:schemaField:(claims,billing_amount)
    Cause: ERROR :: /parent :: "Provided urn claims" is invalid:
    Urn doesn't start with 'urn:'. Urn: claims at index 0: claims
   ```
   Fixed with `urn:li:schemaField:(<full dataset urn>,billing_amount)`.
5. **Assertion descriptions were identical across datasets** — found by
   the repo owner clicking through the actual UI (below), not by any
   automated test. `claims` and `raw_patients` each got their own,
   correctly-scoped assertion (verified: different URNs, different
   run-event histories), but both read
   `"Guardian: billing_amount must never be negative on this dataset."` —
   indistinguishable at a glance in the UI, even though the entities were
   never actually confused under the hood. Fixed by including the entity
   name in the description text itself
   (`"...on claims."` / `"...on raw_patients."`), and manually re-applied
   to the two already-live assertions (re-running Scribe wouldn't have
   updated them — the assertion-definition step is skipped once a URN
   already exists, by design, so an already-defined assertion's
   description doesn't self-heal when the template changes; a direct,
   one-off fix was the right call here rather than changing that
   idempotency behavior mid-fix).

## Hands-on UAT — the repo owner, in the DataHub UI

Following Part D's click-path (localhost:9002 → search `claims` → tag near
the header, Documentation tab, Validation tab; same for `raw_patients`;
`mart_billing` for contrast), the repo owner found real problem 5 above —
something none of the 32 automated `test_scribe.py` tests were positioned
to catch, since none of them assert on description *readability*, only on
correctness (right URN, right dataset, right count). A UI walkthrough with
a human actually looking at it is a genuinely different, complementary
check from an automated test suite asserting on structured data — this
session is direct evidence of that, not just a stated principle.

## Not done in this work package

- **Remediator (US-3), Scribe's audit-report generation (US-5)** — later
  work packages.
- **`OllamaBackend`/`AnthropicBackend`'s Design-A path still has no live
  proof** — unrelated to Scribe (Sprint 2's own gap), unchanged here.
- **`scribe.py` duplicates `investigator.py`'s MCP-session-spawning code**
  rather than sharing a module — decision 0007's own stated consequence,
  not revisited this work package.
- **No lifecycle management for the `guardian-incident` tag or the
  assertion definitions** — nothing removes a tag or retires an assertion
  if an incident is later resolved/fixed. Writeback only, by design; this
  work package didn't build any notion of incident resolution.
- **The GitHub blob links in each doc note require the repo to be pushed
  to resolve** — true as of this commit, once pushed (see below).

## How to run it yourself

```bash
guardian run --segment "Cigna,obesity"          # writes back by default
guardian run --segment "Cigna,obesity" --no-writeback   # Investigator only, no DataHub writes

pytest tests/                                    # 182 tests run, ~1s, zero live calls
pytest tests/test_scribe.py -m live              # the one live Scribe test, real DataHub writes, zero LLM cost
```
