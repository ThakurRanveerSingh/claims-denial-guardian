# Requirements — Claims Denial Guardian

Status: Approved (negotiated scope, captured 2026-07-22)
Source: business-analyst / business-user negotiation, transcribed verbatim by solution-architect.

## Priority order and cut lines

| Priority | Item | Notes |
|---|---|---|
| P0 | **Layer A** — Sentinel detects a denial anomaly → Investigator traces it via DataHub MCP to the planted root cause → Scribe writes findings back to DataHub | This is the demo. If only this works, we still submit a strong entry. |
| P0 | Generated fix code in `examples/` | Judges explicitly want sample artifacts. |
| P1 | Real GitHub PR | Thin layer on top of P0's fix code — the code already exists, we just push a branch and call the GitHub API. |
| P1 | **Layer C** — audit report | Cheap: a Markdown-generation step over data the Investigator already gathered. Large "society-centric" storytelling value. |
| P2 | **Layer B** — model drift monitoring (simplified) | Register the denial model's lineage in DataHub; Sentinel checks one drift signal. Not a full ML observability suite. |
| P3 | Dashboard | Only if ahead of schedule on Aug 5. Realistically: no. |

**Cut-line rule:** on Aug 3, anything P2+ not started gets cut, no debate. Agreed (silence = agreement, as in all real standups).

## The six user stories

### US-1 (Sentinel)
As a claims operations lead, when denial rates spike abnormally for any segment (insurance provider, condition, hospital), I want it detected automatically within one pipeline run, so wrongful denials don't accumulate for weeks.

**Acceptance:** a seeded anomaly in claims data is flagged with segment + magnitude.

### US-2 (Investigator)
As a data engineer, I want the anomaly traced upstream through real lineage (claims ← mart_billing ← staging ← raw) to a named root cause, so I fix causes not symptoms.

**Acceptance:** correctly identifies the planted issue (e.g. negative billing) and the affected branch only — the "selective halting" story.

### US-3 (Remediator)
As a data engineer, I want fix code generated (SQL/dbt-style transformation + validation checks) that matches the actual schema read from DataHub, landing in `examples/` and as a PR.

**Acceptance:** generated code runs against `healthcare.db` without edits.

### US-4 (Scribe)
As the next engineer/agent, I want findings written back to DataHub (incident tag, assertion, documentation note on affected datasets), so knowledge persists in the graph.

**Acceptance:** tags/docs visible in the DataHub UI after a run.

### US-5 (Audit report)
As a compliance officer, I want a human-readable report: what broke, which members' claims were affected, what was done.

**Acceptance:** a Markdown report is generated per incident.

### US-6 (Drift check)
As an ML engineer, I want the denial model's inputs monitored for one drift signal, with its ML lineage visible in DataHub.

**Acceptance:** drift on a seeded shift is flagged and traced to the model via lineage.

## Traceability

| User story | Priority | Primary component |
|---|---|---|
| US-1 | P0 (Layer A) | Sentinel |
| US-2 | P0 (Layer A) | Investigator |
| US-3 | P0 | Remediator |
| US-4 | P0 (Layer A) | Scribe |
| US-5 | P1 (Layer C) | Scribe (audit report generation) |
| US-6 | P2 (Layer B) | Sentinel (drift check) + DataHub (model lineage) |

See `docs/architecture/hld.md` for how these map to system components, and `docs/architecture/lld-sprint1.md` for the Sprint 1 design (schema + toy model + DataHub registration) that the P0/P1 stories depend on.
