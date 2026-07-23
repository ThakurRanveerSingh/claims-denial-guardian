# High-Level Design — Claims Denial Guardian

Status: Draft, expanding the approved architecture sketch
Author: solution-architect subagent
Related: [docs/requirements.md](../requirements.md), [docs/decisions/0001-sdlc-subagent-team.md](../decisions/0001-sdlc-subagent-team.md)

## 1. System overview

Claims Denial Guardian watches a healthcare claims data pipeline, detects abnormal denial patterns, traces them to a root cause using real data lineage, generates a fix, and writes what it learned back into the metadata graph so the knowledge persists. The demo (P0/P1 per `requirements.md`) is a single incident walked end-to-end: **detect → trace → fix → record → report**.

```
healthcare.db (SQLite)                     DataHub (Docker, localhost:8080/9002)
 raw_patients → staging_patients ─┐          metadata, lineage,
                └→ mart_demographics│         tags, glossary
        + NEW: claims, denials     │             ▲    │
        + toy denial model ────────┘  writeback  │    │ reads via
                                      (Scribe)   │    ▼ MCP Server
                             ┌────────────────────┴──────────┐
                             │      Orchestrator (Python)      │
                             │  Sentinel → Investigator →       │
                             │  Remediator → Scribe             │
                             │  (Anthropic SDK, pluggable        │
                             │   model config)                    │
                             └──────┬──────────────┬────────────┘
                                out: CLI narrative  │ examples/<incident>/
                                                    │  fix code + PR + audit_report.md
```

Two things worth naming explicitly, since they're easy to conflate:

- **Runtime agents** (Sentinel, Investigator, Remediator, Scribe) are application code — Python classes in `src/agents/` that call the Anthropic Messages API directly via the `anthropic` SDK. They are what Claims Denial Guardian *is*.
- **SDLC subagents** (business-analyst, solution-architect, senior-dev, ...) are Claude Code subagents in `.claude/agents/` used to *build* Claims Denial Guardian. They never run as part of the shipped product. See decision 0001.

## 2. Components

### 2.1 healthcare.db (SQLite)
The data plane. A 4-stage pipeline (`raw_patients → staging_patients → mart_billing` / `mart_demographics`) built from a public Kaggle healthcare dataset, with data-quality issues deliberately planted at the raw stage and propagated downstream by design (not filtered out in staging — see LLD §1 for what's actually in there). Sprint 1 extends this with `claims`, `denials`, and `denial_model_scores`.

### 2.2 DataHub (Docker)
The metadata plane: dataset schemas, column-level docs, tags, glossary terms, ownership, and — most importantly for this project — **lineage**, the graph edges that let Investigator trace an incident from a mart table back to its raw source. Confirmed running locally (GMS on :8080, UI on :9002).

### 2.3 DataHub MCP Server (reads) / SDK emitter (writes)
Per `CLAUDE.md`: all DataHub metadata **reads** go through the MCP server or SDK — never a hardcoded schema assumption. In practice this means Sentinel and Investigator query DataHub (schema, lineage, existing tags/assertions) via the MCP server at runtime rather than assuming column names. **Writes** (Scribe's incident writeback, and the one-time Sprint 1 registration step) go through the `acryl-datahub` Python SDK's REST emitter — the same mechanism the existing `add_lineage.py` / `add_metadata.py` scripts already use (see LLD §3). This read/write split matches the two arrows in the diagram.

### 2.4 Orchestrator (Python, `src/agents/`)
Runs the four-stage pipeline in sequence for a detected incident. Each stage is a separate class with a narrow, testable responsibility:

| Stage | Responsibility | DataHub interaction | Default model |
|---|---|---|---|
| **Sentinel** | Compute denial rates per segment, flag statistical outliers, summarize in plain language | Read (MCP): confirms schema before querying | **Haiku** |
| **Investigator** | Trace the flagged segment upstream through lineage to a specific root cause; stop at the affected branch only ("selective halting") | Read (MCP): walks lineage graph | **Sonnet** |
| **Remediator** | Generate fix code (SQL/dbt-style transform + validation checks) matching the real schema | Read (MCP): confirms exact column names/types before generating code | **Sonnet** |
| **Scribe** | Write incident tag + assertion + doc note back to DataHub; assemble `audit_report.md` | Write (SDK emitter) | **Haiku** |

**Rationale for the tiering:** this mirrors `CLAUDE.md`'s policy ("default Sonnet; Opus only for HLD/LLD review and hard debugging; Haiku for tests, docs, scaffolding, bookkeeping") applied per pipeline stage instead of per SDLC role. Sentinel's job is mostly a deterministic statistical check with a natural-language summary wrapped around it — cheap, Haiku-appropriate. Investigator and Remediator are where correctness actually matters (a wrong root cause or fix code that doesn't run against the real schema defeats the whole demo) — they get Sonnet. Scribe is templated bookkeeping — writing structured findings and assembling a Markdown report — which is explicitly the kind of task `CLAUDE.md` assigns to Haiku.

**Escalation path:** the config is "pluggable" per the original sketch, not hardcoded. If Investigator or Remediator fail twice on the same incident (ambiguous root cause, generated fix doesn't validate), the orchestrator should support a one-off override to Opus for that specific stage/run — consistent with `CLAUDE.md`'s "Opus only for... hard debugging" carve-out. This is a Sprint 2+ concern (retry/escalation logic), not Sprint 1.

### 2.5 Output artifacts
- **CLI narrative** — real-time human-readable log of what each stage is doing, for the live demo.
- **`examples/<incident-id>/`** — generated fix code (US-3) and, once P1 ships, the pushed branch/PR. Grouping fix code and its audit report under one incident-scoped folder (see below) keeps everything about a single incident together rather than inventing multiple top-level output locations.
- **`examples/<incident-id>/audit_report.md`** — the compliance-facing Markdown report (US-5). Proposed location, not yet in the Sprint 0 scaffold; flagged as an open question below.

## 3. Control flow (high level)

One incident = one sequential pass through Sentinel → Investigator → Remediator → Scribe. Investigator's trace is deliberately **selective**: it should stop at the specific branch actually implicated (e.g. the `mart_billing` branch, when the anomaly is billing-related) rather than walking the entire lineage graph — this is the "selective halting" behavior named explicitly in US-2's acceptance criteria, and it's also the design point of the underlying `healthcare.db` fixture (see LLD §1): a naive circuit breaker halts everything on any quality issue; a smart one halts only the affected downstream branch.

Full retry/failure-handling design (what happens when Investigator can't find a root cause, when Remediator's generated code fails validation, escalation to Opus) is out of scope for this HLD — it belongs in a Sprint 2 LLD once the P0 happy path is proven end-to-end.

## 4. Open questions / risks (found during Sprint 1 grounding)

These came up while writing the Sprint 1 LLD and affect the system as a whole, so they're recorded here rather than buried in the sprint doc:

1. **Base lineage doesn't exist yet.** The DataHub instance has `raw_patients` / `staging_patients` / `mart_billing` / `mart_demographics` registered as bare schema entities (ingestion ran), but **no lineage edges between them** — `add_lineage.py` and `add_metadata.py` from the fixture kit have not been run. Without this, Investigator's trace would dead-end at `mart_billing` even after claims/denials are added. This is now a Sprint 1 prerequisite, not an assumption — see LLD §3.
2. **`hospital` is too high-cardinality to be a primary anomaly-detection segment.** 39,876 distinct hospitals across 55,500 rows — most hospitals have 0-2 rows. Grouping by (provider, condition, **hospital**) as a 3-way segment, as US-1 literally lists it, produces mostly-empty buckets with no meaningful baseline rate. Recommendation for Sentinel's eventual design (Sprint 2+): use **(insurance_provider, medical_condition)** — 5 × 6 = 30 segments, ~1,850 rows each — as the primary detection grouping, and treat `hospital` as a drill-down/narrative detail once a segment is already flagged, not a grouping key. Flagged here because it affects how Sprint 1's seeded demo data should be shaped (LLD §2) and will affect Sentinel's actual query design later.
3. **`examples/<incident-id>/audit_report.md` is a proposed location, not a decided one.** `docs/walkthroughs/` is reserved by `CLAUDE.md` for sprint retrospectives, not per-incident output, so it's the wrong place for this. Revisit if it turns out judges expect audit reports somewhere more prominent (e.g. repo root).

## 5. Non-goals for this phase

- Dashboard (P3) — not designed until P0-P2 are done and there's schedule slack.
- Full ML observability for the denial model (US-6 says "not a full ML observability suite" explicitly) — Sprint 1 only builds the substrate (model + scores + lineage); the drift-check logic itself is P2, Sprint 2+.
- Retry/escalation orchestration logic — noted in §3, deferred.
