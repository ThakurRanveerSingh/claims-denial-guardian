# 0003 — DataHub MCP server: dev environment and product design

Date: 2026-07-23
Status: Accepted

(Named `mcp-integration.md` in the original ask; numbered `0003-` here to
match this folder's existing convention from decisions 0001/0002.)

## Context

Two separate things needed the DataHub MCP server wired in:

1. **Dev environment**: so Claude Code (this tool) can read DataHub's schema/
   lineage/metadata directly during development, instead of everyone pasting
   GraphQL query results into chat by hand.
2. **Product design**: `CLAUDE.md` already states the rule — "All DataHub
   metadata reads MUST go through the DataHub MCP server or SDK — never
   hardcode schemas" — and the HLD (§2.3) already specified a read/write
   split (MCP/SDK for reads, SDK emitter for writes) before any of this was
   actually wired up or tested. This session makes that design real and
   verifies it against the actual running server, rather than leaving it as
   an untested assumption.

## Decision

**Dev environment**: registered `acryldata/mcp-server-datahub` (the official
DataHub MCP server) with Claude Code at **project scope** (`.mcp.json`, run
via `uvx mcp-server-datahub@latest`), authenticated with a DataHub personal
access token. `.mcp.json` uses `${DATAHUB_GMS_URL}`/`${DATAHUB_GMS_TOKEN}`
expansion syntax rather than literal values, since `.mcp.json` is meant to be
committed — the real values stay in `.env` (gitignored), matching
`CLAUDE.md`'s existing secrets rule.

**Product design**: the runtime agents (Sentinel, Investigator, Remediator —
per HLD §2.4) will consume this exact same MCP server, not a separate
integration built for the product. Each agent that needs to read DataHub
metadata spawns `mcp-server-datahub` as a subprocess over stdio using the
official `mcp` Python client SDK (now a project dependency, confirmed working
against the real server this session — see the smoke test below), configured
from the same `DATAHUB_GMS_URL`/`DATAHUB_GMS_TOKEN` in `.env` (loaded via
`python-dotenv`, already a dependency). This is independent of Claude Code's
own `.mcp.json` — that file configures *this dev tool's* connection; the
product's agents make their own MCP client connection in their own Python
code at runtime.

**Smoke test, verified working (see `docs/walkthroughs/sprint-1-day1.md`-
adjacent session notes for raw output)**: `search` returned all 11 registered
datasets with correct tags/glossary/ownership facets; `get_lineage` on
`denials` (upstream, 2 hops) correctly returned `claims` at degree 1 and
`mart_billing`/`mart_demographics` at degree 2 — exactly the lineage chain
Sprint 1 built.

## Why MCP instead of direct SDK calls, for reads

1. **Hackathon judging criterion.** Using MCP for agent-tool integration
   (rather than hand-rolled API/SDK calls scattered through agent code) is
   itself something this hackathon's judging rubric values — it's not just
   an implementation detail, it's part of what's being evaluated.
2. **Agent-portable context.** The MCP server's tools (`search`,
   `get_lineage`, `get_entities`, `list_schema_fields`, ...) are a
   standardized interface with built-in pagination and token-budget
   management (confirmed at runtime: the server logs config knobs like
   `TOOL_RESPONSE_TOKEN_LIMIT` and `ENTITY_SCHEMA_TOKEN_BUDGET`) — designed
   specifically for LLM agents consuming DataHub, not for arbitrary
   application code. Any agent that speaks MCP gets this for free, in any
   runtime, without re-deriving DataHub's GraphQL schema or reimplementing
   pagination/truncation logic per agent. The same tool surface Claude Code
   used during this dev session is exactly what Sentinel/Investigator see at
   runtime — dev-time exploration and product behavior stay consistent.
3. **Directly enforces the "never hardcode schemas" rule.** A hand-rolled
   GraphQL query (what Sprint 1's `add_lineage.py`/`add_metadata.py`/
   `register_ml_model.py` do) still risks silently assuming field shapes.
   The MCP server's tools return live, current schema/lineage — there's no
   local assumption to go stale.

## Where the SDK is still used: writebacks

Mutation tools are **disabled by default** in `mcp-server-datahub` — confirmed
directly in this session's server boot log: `"Mutation Tools DISABLED"`,
`"User Tools DISABLED"`, `"Data Quality Tools DISABLED"`. (The server does
expose a `TOOLS_IS_MUTATION_ENABLED` flag to turn mutations on, per its
README — not used here.)

Writes stay on the `acryl-datahub` Python SDK's REST emitter
(`DatahubRestEmitter`/`MetadataChangeProposalWrapper`), the same mechanism
already proven across Sprint 1's `add_lineage.py`, `add_metadata.py`, and
`register_ml_model.py`. This applies to:
- **Scribe's incident writeback** (HLD §2.4) — tags, assertions, doc notes.
- **One-time registration/setup scripts** — schema/lineage/metadata
  registration, the kind of work already done and tested this session.

## Alternatives considered

- **All reads via direct SDK/GraphQL calls (what Sprint 1's setup scripts
  already do).** Rejected as the ongoing pattern for the *product's runtime
  agents* — every agent would hand-roll its own DataHub queries and
  implicitly assume schema shapes, exactly what `CLAUDE.md`'s rule exists to
  prevent. Fine for one-time setup scripts (already built, already tested);
  not the pattern for agents making repeated, varied reads at runtime.
- **All reads and writes via MCP (enable mutation tools).** Rejected —
  mutation tools are explicitly a separate, disabled-by-default surface in
  this server, and Sprint 1 already has working, tested SDK-based write code
  (`TrainingDataClass`, `MLModelPropertiesClass`, `DatasetPatchBuilder`
  patches, etc.). Switching writes to MCP now would mean redoing
  already-working code for a mutation interface we haven't evaluated, for no
  clear benefit over the fine-grained control the SDK already gives.

## Consequences

- `.mcp.json` is safe to commit (no secrets in it); `.env` remains the single
  place real credentials live, consistent with `CLAUDE.md`.
- Enabling DataHub's token-based auth (`METADATA_SERVICE_AUTH_ENABLED`,
  previously off) means Sprint 1's existing scripts
  (`add_lineage.py`/`add_metadata.py`/`register_ml_model.py`) will now need a
  token to keep working, since anonymous GMS access no longer works. Not
  updated in this session — flagged here as a follow-up.

  **Resolved 2026-07-23**: all three scripts now `load_dotenv()` and read
  `DATAHUB_GMS_URL`/`DATAHUB_GMS_TOKEN` from `.env`, passing `token=` into
  both `DataHubGraph`/`DatahubClientConfig` and `DatahubRestEmitter`; each
  exits with a clear error (not a raw `401`/traceback) if the token is
  missing. `ingest.yaml`'s `datahub-rest` sink now sets
  `token: "${DATAHUB_GMS_TOKEN}"`, resolved by `datahub ingest`'s own env-var
  expansion against the process environment. Verified live: with the token
  absent, `add_lineage.py --dry-run` fails fast with the new error message
  (exit 1, confirmed by temporarily moving `.env` aside); with it present, a
  real (non-dry-run) `add_lineage.py` run against the local GMS succeeded —
  9/9 lineage edges emitted, matching the 401 seen pre-fix on the same
  anonymous request. See `src/datahub/README.md#authentication` for the
  setup steps (enabling the flag, generating a token in the DataHub UI, the
  required `.env` variables).
- The product's agents will each need to manage their own MCP client
  lifecycle (spawn `mcp-server-datahub`, initialize session, call tools) —
  this is a real implementation dependency for Sentinel/Investigator, not
  yet built (agent implementation is still ahead of Sprint 1's current
  scope).
