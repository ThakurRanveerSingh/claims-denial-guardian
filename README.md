# Claims Denial Guardian

## The pitch

An agent pipeline that watches a healthcare claims pipeline, statistically
flags abnormal denial patterns, traces each one to a root cause through
**real DataHub lineage** (not a guess), generates a fix, writes what it
learned back into the metadata graph, and produces a compliance-facing
audit report — end to end, against real (synthetic) data, every claim
checked against a live system rather than assumed. Two seeded incidents
prove Investigator actually discriminates, not just pattern-matches: one
defect is injected *downstream*, directly into `claims`
(`UnitedHealthcare`/`diabetes` → traced to `introduced_at:claims`), the
other *upstream*, into the original source data
(`Cigna`/`obesity` → traced all the way to `inherited_from:raw_patients`)
— same detector, same investigator, two structurally different answers,
both verified correct against the real lineage graph.

Hits all four tracks: **Agents That Do Real Work** (Sentinel/Investigator/
Scribe actually detect, trace, and write back against a live DataHub
instance — nothing here is scripted or mocked for the demo); **Metadata-
Aware Code Generation & Development** (Remediator reads the real, live
DataHub schema before generating fix SQL, then opens a real PR —
[#1](https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/1)/[#2](https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/2)
— against it); **Production ML Agents** (a real `MLModel`/`MLFeature`/
`MLFeatureTable` entity registered in DataHub, checked on demand by
`guardian check-drift` against its own documented feature-health
invariants); and **Open/Wildcard** (the FHIR compliance bridge —
`guardian export-fhir` generates real FHIR R4 resources traceable back to
the specific incident and DataHub lineage that flagged the data they're
built on, a CMS-0057-F compliance-linkage angle nothing else here covers).

## Quick links

- [`examples/`](examples/) — real, already-generated output for both
  canonical incidents: `incident.json`, audit reports, FHIR resources.
- Sample report: [`examples/INC-20260724T234736Z-cigna-obesity/report/audit_report.html`](examples/INC-20260724T234736Z-cigna-obesity/report/audit_report.html)
  (GitHub shows source here — it's a real, self-contained HTML file;
  download/open it locally to see it rendered).
- Real, open pull requests Remediator filed:
  [PR #1](https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/1) ·
  [PR #2](https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/2)
- Worth reading first: [`docs/decisions/0008`](docs/decisions/0008-remediator-design.md)
  (why Remediator quarantines suspect rows instead of "fixing" values it
  can't actually verify) and [`docs/decisions/0012`](docs/decisions/0012-fhir-compliance-bridge.md)
  (exactly what the FHIR export is and isn't — written for a reader who
  knows FHIR).

**Current state, honestly**: 414 tests (408 pass by default; 6 marked
`@pytest.mark.live`, excluded unless you point them at a real DataHub —
see Testing below). Every live-path number in this README was measured,
not assumed: a zero-config `guardian run` takes **158.5s**, `guardian
check-drift` **3.4s**, `guardian export-fhir` **1.8s** — all down from
multi-minute runs before DataHub's own telemetry was found and disabled
(full before/after: [`docs/walkthroughs/sprint-4-wp1.md`](docs/walkthroughs/sprint-4-wp1.md)).

**Sentinel** (detect) → **Investigator** (trace root cause via DataHub
lineage) → **Remediator** (generate + open a real PR) → **Scribe** (write
findings back to DataHub) → **Reporter** (audit report) → **Drift**
(model feature-health check) → **FHIR export** (CMS-0057-F
compliance-linkage demo).

Full design: [`docs/architecture/hld.md`](docs/architecture/hld.md).
Every non-trivial decision made while building this is logged in
[`docs/decisions/`](docs/decisions/); every sprint has a
[`docs/walkthroughs/`](docs/walkthroughs/) entry covering what was built,
why, and what was rejected.

---

## Prerequisites

- **Python 3.11+**
- **Docker** (to run DataHub locally — see below)
- **One LLM backend** — pluggable via `.env`'s `LLM_BACKEND`, three named options:
  - `claude_code` (**the default**) — the [`claude` CLI](https://docs.claude.com/claude-code) on `PATH`, already logged in. **No `ANTHROPIC_API_KEY` needed**, it uses your existing Claude subscription/login.
  - `anthropic` — a real, working `ANTHROPIC_API_KEY` in `.env`.
  - `ollama` — interface-only stub today (no local-model integration built yet); listed here for honesty about the pluggable-backend shape, not because it currently runs anything.
- `gh` CLI, authenticated, only if you want to reproduce Remediator's pull
  requests (`--remediate`) — optional, the demo PRs already exist on
  [`denial-guardian-data-platform`](https://github.com/ThakurRanveerSingh/denial-guardian-data-platform).

## Setup

### 1. Install

```bash
git clone https://github.com/ThakurRanveerSingh/claims-denial-guardian.git
cd claims-denial-guardian
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
# Optional: nicer terminal output (colorized summaries). Everything works
# without it — the plain-text fallback is a real, tested code path, not an
# afterthought.
pip install -e ".[rich]"
```

This installs a real `guardian` command on `PATH` (and `datahub`, via the
pinned `acryl-datahub` dependency — you don't need to install DataHub's CLI
separately).

### 2. Bring up DataHub (Docker)

Everything past `guardian run --dry-run` needs a live local DataHub
instance — Investigator traces lineage through it, Scribe/Drift/FHIR-export
write findings back to it.

```bash
datahub docker quickstart
```

This pulls and starts DataHub's full container stack. First run takes a
few minutes. Once it's up: GMS (the API Investigator/Scribe talk to) is at
`http://localhost:8080`, the UI is at `http://localhost:9002`
(default login `datahub` / `datahub`).

Then ingest this project's dataset and metadata (from `src/datahub/`; see
[`src/datahub/README.md`](src/datahub/README.md) for what each script
does and the full troubleshooting section):

```bash
cd src/datahub
datahub ingest -c ingest.yaml
python add_lineage.py
python add_metadata.py
python register_ml_model.py
cd ../..
```

**Generate a Personal Access Token** (DataHub UI → **Settings → Access
Tokens → Generate new token**, copy it immediately) and put it in `.env`
(see below). *Note, checked directly against this project's own
quickstart instance: reads and writes both currently succeed even with no
token at all — a token is still recommended (it's what identifies who
made a change in DataHub's own audit trail), and a differently-configured
or production DataHub instance may enforce it strictly. Don't rely on the
open-by-default behavior.*

### 3. `.env`

```bash
# Required for anything past `guardian run --dry-run`
DATAHUB_GMS_URL=http://localhost:8080
DATAHUB_GMS_TOKEN=<paste the token from step 2>

# Optional — defaults shown. Uncomment to override.
# LLM_BACKEND=claude_code          # claude_code | anthropic | ollama (ollama is an interface-only stub)
# ANTHROPIC_API_KEY=               # only read if LLM_BACKEND=anthropic
# ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
# INVESTIGATOR_MAX_BUDGET_USD=2.0
# INVESTIGATOR_MAX_TURNS=15
# SENTINEL_Z_THRESHOLD=3.5
# DATAHUB_TELEMETRY_ENABLED=false  # already the effective default (see note below); set =true to opt back in

# Only needed for `guardian run --remediate` (opens a real PR) — optional,
# the demo PRs already exist.
# DATA_PLATFORM_REPO_PATH=~/projects/denial-guardian-data-platform
```

`.env` is gitignored — never commit real tokens/keys.

**Telemetry note**: `acryl-datahub`'s own telemetry (unrelated to this
project) tries to phone home on most DataHub operations; on a network that
can't reach it, each call burns real time in connection-timeout retries.
Every module here sets `DATAHUB_TELEMETRY_ENABLED=false` by default (a
`setdefault`, so an explicit `.env` value still wins) — nothing to do here,
just explaining why live commands are faster than you might otherwise
expect from a project this DataHub-heavy.

## Quickstart

```bash
# Zero cost, zero config beyond having the committed healthcare.db present
# (already in the repo) — scans every segment, prints what WOULD be
# investigated, spends nothing.
guardian run --dry-run

# The real thing: flags anomalous segments, investigates the root cause
# via live DataHub lineage, writes findings back, generates an audit
# report. Can take a few minutes (mostly DataHub round-trips) — prints a
# progress line before the long part, not silent.
guardian run --segment "Cigna,obesity"

# Or let it pick whatever Sentinel actually flags, across all segments:
guardian run

# A single, on-demand model feature-health check (zero LLM):
guardian check-drift --incident <incident-id-from-the-run-above>

# CMS-0057-F compliance-linkage demo: sample FHIR R4 ExplanationOfBenefit
# resources for an already-investigated incident, registered in DataHub
# with real lineage back to raw_patients. Structural demo only — see
# docs/decisions/0012 for exactly what this is and isn't.
guardian export-fhir <incident-id>

# Resume a previously saved incident from a later stage, without
# re-running Sentinel/Investigator (which would mint a new incident and a
# new PR instead of updating the existing one):
guardian resume <incident-id> --stage writeback
```

Every real run writes `examples/<incident-id>/incident.json` plus
`examples/<incident-id>/report/audit_report.{md,html}` — the HTML report
is fully self-contained (inline CSS, system fonts, no external
dependencies) and renders correctly with no network connection at all.

Two real, already-investigated incidents are committed under `examples/`
if you want to look at real output before running anything yourself.

## Testing

```bash
pytest tests/
```

No network calls by default — tests hitting the real DataHub instance are
marked `@pytest.mark.live` and excluded (`pytest.ini`'s `addopts = -m "not
live"`). Run them explicitly with `pytest tests/ -m live` once DataHub is
up and `.env` is configured.

## The sibling repo

Remediator's generated fixes land as real pull requests on
[`denial-guardian-data-platform`](https://github.com/ThakurRanveerSingh/denial-guardian-data-platform)
— a separate repo simulating the data-platform team Guardian is filing
fixes against. PR [#1](https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/1)
and [#2](https://github.com/ThakurRanveerSingh/denial-guardian-data-platform/pull/2)
are the real, already-open results for this project's two canonical
incidents — nothing more to set up there, they're just pull requests on
GitHub.

## What this is, and isn't

Built end-to-end against real synthetic data, a real local DataHub
instance, and (where used) a real GitHub repo — not mocked for the demo.
Every module states plainly what it does and doesn't cover; the FHIR
export in particular ([`docs/decisions/0012`](docs/decisions/0012-fhir-compliance-bridge.md))
is explicit about being a compliance-*linkage* demonstration, not
production FHIR conformance. `docs/decisions/` has the full record of
every place a shortcut was considered and rejected, and why.
