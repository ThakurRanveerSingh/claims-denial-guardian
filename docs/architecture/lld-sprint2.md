# Low-Level Design — Sprint 2

Status: Draft — ready for review before implementation
Scope: the agent core — Sentinel (US-1), Investigator (US-2), and the
Orchestrator that runs them as a two-stage pipeline with a CLI entrypoint.
LLM backend pluggability (`src/agents/llm_backend.py`) is designed here too,
since Investigator's design depends on it. **No implementation code in this
document — design only**, same convention as `lld-sprint1.md`.
Out of scope for Sprint 2: Remediator (US-3), Scribe (US-4/US-5), drift
checking (US-6), any DataHub *writeback* (that's Scribe's job, a later
sprint — this sprint's agents only read).

## 0. Grounding — what's actually there

Sprint 1 is done and pushed. Before designing anything, this session verified
the following against the real environment rather than assuming it.

**DataHub is fully populated.** `add_lineage.py` ran for real this session,
authenticated via `DATAHUB_GMS_TOKEN` — all 9 lineage edges emitted. The
graph Investigator will walk actually exists:
`raw_patients → staging_patients → mart_billing`/`mart_demographics →
claims → denials`/`denial_model_scores`, with tags/glossary/ownership per
`lld-sprint1.md` §4, plus a real `MLModel` entity (`denial_risk_model`,
platform `python`) and `MLFeatureTable` (`denial_risk_features`) linked via
`mlFeatures`. **One caveat that matters for how Investigator's lineage-walk
is scoped**: no tested DataHub mechanism in this version makes an `MLModel`
a graph-traversable upstream of a `Dataset` — both `UpstreamLineageClass`
and the `updateLineage` GraphQL mutation reject non-`Dataset` URNs, confirmed
against the live GMS. The model → `denial_model_scores` relationship is
recorded as a `customProperties` note (`produced_by_model`), not a lineage
edge. Irrelevant to US-1/US-2's happy path (a billing anomaly, not model
drift), but Investigator's lineage-walk logic should not assume it can ever
traverse *through* an `MLModel` node — asking `get_lineage` to do that will
just return nothing, which is correct, expected behavior, not a bug to
special-case.

**The DataHub MCP server's actual read-only tool surface**, confirmed live
this session against the same server (`acryldata/mcp-server-datahub`) the
product's agents will spawn per decision 0003: `search`, `get_entities`,
`get_lineage`, `get_lineage_paths_between`, `list_schema_fields`,
`get_dataset_queries`, `grep_documents`, `search_documents`. Mutation tools
are disabled by default (confirmed in the server's boot log) — consistent
with decision 0003's read-via-MCP/write-via-SDK split. Investigator only
needs reads, so this tool surface is sufficient as-is; nothing new needs to
be added to the MCP server.

**Auth path for the product's own MCP connection, verified concretely this
session.** Per decision 0003, the product's agents spawn their own
`mcp-server-datahub` subprocess over stdio via the `mcp` Python SDK,
independent of Claude Code's own `.mcp.json`. The open question flagged for
this LLD was whether `claude -p --mcp-config <file>` supports the same
`${VAR}`-style expansion `.mcp.json` uses, or needs literal secret values
assembled at invocation time. **Verified directly**: wrote a minimal MCP
config with `"MY_TEST_VAR": "${MY_TEST_VAR}"` in a fake stdio server's `env`
block, pointed a throwaway Python "server" at it that dumps its received
`os.environ` to a file and exits, set `MY_TEST_VAR=hello_world_12345` in the
calling shell, and ran
`claude -p "..." --mcp-config <file> --strict-mcp-config --output-format json`.
The dumped file showed the real value, not the literal `${MY_TEST_VAR}`
string. **It expands, exactly like `.mcp.json`.** This resolves §2.6 below
concretely: Design B's MCP config can be a small, static, committed file,
not a per-run temp file with substituted secrets.

**The same smoke test also produced a real cost data point.** A prompt that
made zero tool calls still cost `$0.1130436` (`17,894` cache-creation input
tokens, `15,912` cache-read input tokens, `0` real output tokens — it hit a
deliberately tiny `--max-budget-usd 0.05` cap and stopped via
`"terminal_reason":"budget_exhausted"`). That's Claude Code's own system
prompt and MCP/tool config being loaded into context — a **fixed cost per
subprocess spawn**, independent of how much real work happens inside it.
This number directly shaped the Design A vs. B recommendation in §2.4 and
the cost guardrails in §7 — see decision 0004 for the full reasoning.

**The `claude` CLI is installed and working** (`v2.1.218`). Confirmed via
`--help`: `-p/--print`, `--output-format json`, `--mcp-config <configs...>`,
`--strict-mcp-config`, `--permission-mode <acceptEdits|auto|bypassPermissions|
manual|dontAsk|plan>`, `--allowedTools`/`--disallowedTools`,
`--max-budget-usd <amount>`. **No `--max-turns` flag exists in this CLI
version's `-p` mode** — confirmed absent from `--help` output. This is a
real, named gap for Design B, not an oversight — see §5 and §7.

**The empirical finding that reshaped Investigator's design.** Queried
`healthcare.db` directly rather than trusting `generate_denials.py`'s stated
intent. The seeded anomaly segment (`UnitedHealthcare`, `diabetes`, per
`generate_denials.py`'s `SPIKE_SEGMENT`) has **1,806 claims, 371 denied**
(rate 20.5%, vs. the next-highest of the other 29 segments at 4.05% —
`Cigna`/`arthritis`, 77/1900). Breaking the 371 denials down by
`denial_reason_code`:

| Reason code | Count | Share |
|---|---|---|
| `INVALID_BILLING_AMOUNT` | 361 | 97.3% |
| `RANDOM_AUDIT` | 6 | 1.6% |
| `HIGH_RISK_SCORE` | 4 | 1.1% |

The 361 `INVALID_BILLING_AMOUNT` denials are the ones worth tracing (the
other 10 are the dataset's small background rate, unrelated to this
segment — see §2.2 for why Investigator checks this breakdown before picking
a hypothesis, instead of assuming it). Joining those 361 claims back to their
`mart_billing` source row via `claims.source_billing_rowid = mart_billing.rowid`
(the documented, verified-reliable join per `lld-sprint1.md` §0):

- **36 of 361** are *also* negative in `mart_billing` (≈2.0% of the segment —
  exactly the fixture's general baseline defect rate, propagating cleanly
  from `raw_patients` through the pipeline, as expected).
- **325 of 361** are negative in `claims` but still *positive* in their own
  `mart_billing` source row.

Reading `generate_denials.py` confirms why: `seed_segment_spike()` runs an
`UPDATE claims SET billing_amount = ...` directly against the already-
populated `claims` table — it never touches `mart_billing`, `staging_patients`,
or `raw_patients`.

**What this means for the design, stated plainly:** an Investigator that
assumes "the defect must be upstream, keep walking to `raw_patients`" reaches
the wrong conclusion for 90% of this segment's flagged claims —
`mart_billing`/`staging_patients`/`raw_patients` are clean for those rows.
§2 designs Investigator's hypothesis-testing step around this directly: "does
the anomaly already exist in the immediate upstream source, or does it first
appear here?" is a named, general capability (checked at every hop, not
assumed in either direction), not a one-off special case for this demo's
particular seed. The correct, evidence-backed root cause this sprint's seeded
data actually supports is **"introduced during `claims` derivation, not
inherited from raw source data — `mart_billing`/`staging_patients`/
`raw_patients` require no remediation"** for 325/361 claims, with the
remaining 36/361 correctly attributed to the fixture's separate, pre-existing
baseline defect rate rather than folded into the same finding.

**No `ANTHROPIC_API_KEY` or `LLM_BACKEND` in `.env` yet** — both new this
sprint, need to be added before `AnthropicBackend`/backend selection work.
`anthropic==0.118.0` and `mcp==1.28.1` are already `requirements.txt`
dependencies (added Sprint 1 for this purpose, per decision 0003). `ollama`
is not installed on this machine — consistent with `OllamaBackend` being
interface-only this sprint.

**`src/agents/` and `src/codegen/` are currently empty** (just `.gitkeep`,
no `__init__.py` anywhere in the repo yet). Nothing to preserve or migrate.
No `pyproject.toml`/`setup.py` exists yet either — relevant to §4.3's CLI
entrypoint design.

## 1. Sentinel — anomaly detection

### 1.1 Segment definition (settled, not reopened here)

`(insurance_provider, medical_condition)` — 30 segments, ~1,740-1,907 claims
each (confirmed via query; sizes vary, they are not identical). This was
decided in `lld-sprint1.md` §2 and flagged as a cross-cutting decision in
`hld.md` §4 risk 2 specifically because it would matter for Sentinel's real
design. It's cited here as settled, not re-litigated: `hospital` has 39,876
distinct values across 55,500 rows, mostly buckets of size 1-2 — no
meaningful baseline to detect a spike against.

### 1.2 Detection method: two-proportion z-test, explicit threshold, zero LLM

For each segment, compare its denial rate against the pooled rate of every
*other* segment (leave-one-out — see rationale below), using a standard
two-proportion significance test:

```
p_segment      = segment_denials / segment_claims
p_rest         = (total_denials - segment_denials) / (total_claims - segment_claims)
p_pool         = (segment_denials + (total_denials - segment_denials)) / total_claims
se             = sqrt( p_pool * (1 - p_pool) * (1/segment_claims + 1/(total_claims - segment_claims)) )
z              = (p_segment - p_rest) / se

flagged        = z > Z_THRESHOLD    # default 3.5, a config value (SENTINEL_Z_THRESHOLD)
magnitude      = z                   # and/or p_segment / p_rest as a "Nx baseline" phrase
```

**Verified against the real data**, computed this session: the seeded
segment scores **z = 38.00** (rate 20.54% vs. leave-one-out baseline 3.21%,
≈6.4x baseline). The *next-highest* of the other 29 segments scores
**z = 0.64**. That's not a close call — there's an enormous, clean gap
between "the one seeded anomaly" and "ordinary sampling noise across 30
segments," which is exactly what a threshold-based detector needs to exist
reliably. `Z_THRESHOLD = 3.5` is comfortably clear of that noise ceiling
(0.64) with a lot of headroom — a reasonable, explainable default, not
finely tuned to this dataset's exact numbers. It's a config value
(`SENTINEL_Z_THRESHOLD` in `.env`), not hardcoded, matching
`generate_denials.py`'s own convention of naming implementation-time
parameters explicitly rather than burying them in code.

**Why leave-one-out, not "segment vs. whole population including itself":**
if the flagged segment's own inflated rate were folded into the population
average it's being compared against, a real spike would inflate its own
baseline — diluting the very signal being measured. Excluding the segment
under test from both the baseline rate and the standard-error calculation
avoids that self-contamination.

**Why a z-test and not a simpler fixed-ratio rule (e.g. "flag if rate >
2x average")**: a fixed ratio doesn't account for segment size. A small
segment can swing 2x by pure chance; a large segment's small percentage-point
shift can be highly significant. The `1/n` terms in the standard-error
formula naturally shrink `z` for small segments and grow it for large,
stable ones — this dataset's segments range 1,740-1,907 claims, real
variance, not identical buckets, so this isn't a hypothetical concern. The
z-test gives one consistent, principled threshold across all 30 segments
regardless of size, instead of a magic ratio that happens to work for
average-sized segments and misbehaves at the edges.

**Multiple-comparisons caveat, named rather than ignored**: running 30
simultaneous z-tests technically raises a multiple-comparisons concern —
roughly 30x the single-test false-positive rate for *some* segment crossing
a naive threshold by chance. Not a practical concern here (correcting via
Bonferroni across 30 tests would only move the per-test threshold to
roughly z > 3.7-3.8, an immaterial shift given the actual 38-vs-0.64 gap),
but worth stating as the statistically rigorous caveat rather than silently
skipping it.

**Rejected: a hard minimum-segment-size gate before testing.** Not needed —
the z-test's `1/n` terms already self-correct for small samples (a tiny
segment needs a much larger rate deviation to produce the same z-score as a
large one). Adding a separate hard cutoff would be redundant machinery for a
problem the formula already handles.

### 1.3 `SentinelFinding` — Sentinel's output contract

Illustrative field shape (not code — formalized as a dataclass/TypedDict at
implementation time):

| Field | Type | Notes |
|---|---|---|
| `segment` | `{insurance_provider, medical_condition}` | the flagged (or evaluated) segment |
| `segment_claim_count` | int | |
| `segment_denial_count` | int | |
| `segment_denial_rate` | float | |
| `baseline_denial_rate` | float | leave-one-out, per §1.2 |
| `z_score` | float | the magnitude US-1's acceptance criterion asks for |
| `threshold` | float | the configured `SENTINEL_Z_THRESHOLD` used for this run — recorded, not just applied, so a later reader can audit *why* it was flagged |
| `method` | string | `"two_proportion_z_test"` — named explicitly, auditable |
| `flagged` | bool | |
| `summary` | string | plain-language one-liner, §1.4 |

### 1.4 Where an LLM would (and would NOT) add value here

**The detection decision itself: zero LLM, by design, not by omission.**
Whether `z > 3.5` is a closed-form, deterministic comparison — it has one
exact right answer, computed the same way every time from the same data.
Routing that decision through an LLM would make it slower, non-free, and
non-deterministic (a language model asked to "eyeball whether this rate
looks anomalous" is not reliable at exact arithmetic or threshold comparison
across many rows) for a problem that already has a provably correct,
instantly verifiable closed-form answer. This is exactly the kind of task
where a statistical test is strictly better than an LLM on every axis that
matters here: correctness, cost, latency, reproducibility, and auditability
(a compliance officer or judge can recompute `z` by hand from the numbers in
the finding and get the same answer — they cannot recompute what an LLM
"felt" about the data).

**Where an LLM legitimately helps, if anywhere: narrating the already-
computed result for a human audience.** US-1's persona is a claims
operations lead, not a statistician — "z = 38.00" is not itself an
actionable sentence. `summary` (§1.3) is where a short natural-language
gloss ("Denial rates for UnitedHealthcare diabetes claims are running at
6.4x the typical rate across other segments — this is not normal
variation") adds real value, and this matches `hld.md` §2.4's own framing of
Sentinel exactly: "flag statistical outliers, summarize in plain language."

**Recommendation: this narration step defaults to OFF (a deterministic
string template), not to whichever `LLM_BACKEND` is configured.** This is a
direct consequence of the fixed-overhead cost finding in §0: a bare
`claude -p` call with zero real work still cost $0.11 from system-prompt/
context overhead alone. Spending that (or even `AnthropicBackend`'s smaller
but nonzero per-call cost) on a cosmetic one-liner, on every pipeline run, by
default, is a bad trade. A template (`f"Denial rate for {provider}/
{condition} is {rate:.1%}, {ratio:.1f}x baseline (z={z:.1f}) — statistically
very unlikely to be random variation."`) covers the actual acceptance
criterion ("flagged with segment + magnitude") with zero LLM dependency,
zero added latency, and zero added cost. A `SENTINEL_NARRATION_ENABLED=true`
`.env` flag can opt into an LLM-generated version later if a nicer sentence
is wanted for a live demo — using whichever backend `LLM_BACKEND` already
points at, since this doesn't need a second backend-selection axis, just an
on/off switch on top of the one that already exists.

## 2. Investigator — root-cause tracing

### 2.1 Grounding the workflow in what the data actually shows

§0 already established the concrete finding this design has to account for:
90% of the flagged segment's `INVALID_BILLING_AMOUNT` denials (325/361) have
no matching defect anywhere in the declared upstream chain — the anomaly is
introduced at `claims` itself, not inherited. A design that assumes "keep
walking upstream until you find the planted issue" (a literal reading of US-2's
"claims ← mart_billing ← staging ← raw" acceptance text) gets this
backwards. §2.2 designs the fix: hypothesis-testing that checks reproduction
at each hop, in both directions of belief, as a named general capability —
not a special case wired specifically to this demo's seed values.

### 2.2 Hypothesis-testing workflow (general capability, named steps)

1. **Confirm schema via DataHub MCP** (`list_schema_fields` on `claims`,
   `denials`, and whichever upstream tables get walked) before writing any
   SQL against `healthcare.db` — this is the same "never hardcode schemas"
   rule applied to Investigator's raw-SQL step, not just to which tables
   exist. Since `healthcare.db` is the exact physical database DataHub's
   registered schema describes (auto-introspected at ingestion), the column
   names MCP returns are the column names to use in the query — no
   translation layer needed, just no assuming them without checking first.
2. **Walk lineage upstream from the flagged dataset** (`get_lineage` or
   `get_lineage_paths_between` on `claims`) to get the *actual* registered
   chain, live — don't assume the shape documented in `lld-sprint1.md` §4 is
   still exactly what's in the graph; read it.
3. **Break the flagged segment's denials down by `denial_reason_code`**
   before picking a hypothesis — §0's table above (361 `INVALID_BILLING_
   AMOUNT` / 6 `RANDOM_AUDIT` / 4 `HIGH_RISK_SCORE`) is the general shape of
   what this step produces, not a one-off fact to hardcode. Only
   `INVALID_BILLING_AMOUNT` maps to a known, testable data-quality
   hypothesis (`billing_amount < 0`) in this schema — `RANDOM_AUDIT`/
   `HIGH_RISK_SCORE` denials have no underlying field-level defect to trace
   by design (`generate_denials.py`'s background rule denies an
   already-clean random sample). If the dominant reason code in some future
   segment/dataset turns out to be one of those, Investigator should report
   "elevated rate confirmed, but the dominant reason code has no known
   data-quality hypothesis to test in this schema" rather than forcing a
   billing-amount hypothesis that doesn't apply. This sprint's seeded data
   doesn't exercise that branch (INVALID_BILLING_AMOUNT dominates at 97.3%),
   but the step exists so the design doesn't silently assume every incident
   looks like this one.
4. **Test "does this reproduce at the immediate upstream source?", one hop
   at a time**, starting nearest and walking backward only as far as
   needed: join the flagged claims to their immediate upstream row (here,
   `claims.source_billing_rowid = mart_billing.rowid`, the same verified
   join `lld-sprint1.md` §0 documents) and check whether the implicated
   field's anomaly (`billing_amount < 0`) reproduces there. **Stop at the
   first hop where it stops reproducing** for (near-)all flagged rows — that
   is the general, reusable form of "selective halting," not a special case.
   If it *does* reproduce at a hop, continue one hop further and repeat.
5. **Quantify, don't just classify.** Report how many of the flagged rows
   each explanation actually accounts for (§2.3's `root_cause_breakdown`) —
   collapsing "90% introduced at claims" and "10% inherited from an
   unrelated baseline defect" into one blended answer would misreport both
   halves.
6. **Report `affected_branch` as exactly what the evidence implicates** —
   for this sprint's seeded case, `["claims"]` only, with `mart_billing`,
   `mart_demographics`, `staging_patients`, and `raw_patients` explicitly
   named as "checked, found clean, no remediation needed." This is a
   sharper instance of the "selective halting" story than the literal
   four-stage reading — the branch is one node wide, not four deep.
7. **If evidence doesn't cleanly settle** (reproduction rate at some hop is
   neither ~0% nor ~100%, or a failure mode blocked part of the check),
   return `primary_root_cause: "inconclusive"` with `confidence: "low"` and every
   step actually taken preserved in `evidence` — an honest incomplete
   answer, not a forced-confident guess. See §6, failure mode 5.

### 2.3 `InvestigatorFinding` — Investigator's output contract

| Field | Type | Notes |
|---|---|---|
| `primary_root_cause` | string (enum-like) | `"introduced_at:<dataset>"` \| `"inherited_from:<dataset>"` \| `"inconclusive"` — classifies whichever explanation covers the majority of flagged claims |
| `root_cause_breakdown` | list of `{classification, claim_count, pct, note}` | full accounting — e.g. two entries for this sprint's seeded case: 325/90.0% `introduced_at:claims`, 36/10.0% `inherited_from:mart_billing (baseline defect, unrelated to segment spike)` |
| `affected_branch` | list of dataset names | exactly what needs remediation — `["claims"]` for this sprint's seeded case, not the full chain |
| `datasets_checked_and_clean` | list of dataset names | named explicitly, not just omitted — `["mart_billing", "mart_demographics", "staging_patients", "raw_patients"]` here |
| `lineage_path_walked` | list of dataset names | what `get_lineage`/`get_lineage_paths_between` actually returned, live |
| `evidence` | ordered list of `{step, tool, query_or_call, result_summary}` | every check actually made, in order — this is what makes an "inconclusive" result useful to a human picking up where Investigator stopped |
| `root_cause_summary` | string | plain-language narrative citing the evidence, for the CLI/report |
| `confidence` | `"high"` \| `"medium"` \| `"low"` | |
| `backend_used` | string | `"claude_code"` \| `"anthropic"` \| `"ollama"` — which one actually ran this investigation |
| `turns_used` | int or `null` | populated for the Design A path (§2.4); `null` for Design B since `claude -p`'s internal turn count isn't exposed the same way — see §5 |

Downstream propagation note, stated once here rather than re-derived per
run: `denials` and `denial_model_scores` are downstream of `claims` and
therefore inherit its bad rows — that's expected propagation, not a second
defect. `affected_branch` names where the defect *originates*, not every
dataset that happens to contain affected rows as a consequence.

### 2.4 The hard trade-off: our own agent loop vs. delegating to `claude -p`

Evaluated in full in **decision 0004** — summarized here because it's the
load-bearing call for this section's design.

**Recommendation: Design B (delegate the whole investigation to one
`claude -p --mcp-config ... --output-format json` call) specifically for
`ClaudeCodeBackend`. Design A (our own turn-by-turn loop, calling the
DataHub MCP relay and a read-only `healthcare.db` query tool ourselves) for
`AnthropicBackend` and `OllamaBackend`.**

This is not a hedge — it's what the evidence in §0 actually supports.
`claude -p` is not a bare completion endpoint the way the Anthropic Messages
API or a local Ollama model is; it's already a complete agent harness with
its own tool loop and MCP client. Running Design A's loop *around*
`ClaudeCodeBackend` would mean spawning a fresh `claude -p` subprocess per
turn (each paying the ~$0.11 fixed context-loading cost measured in §0) to
recreate — worse, with no structured-output guarantee — a tool loop
`claude -p` already runs internally, once, for free (on the repo owner's
existing Pro subscription quota). Conversely, `AnthropicBackend` and
`OllamaBackend` have no equivalent harness to delegate to — Design A is the
only shape that makes sense for a bare completion API. Full comparison table
(debuggability, portability, cost, failure isolation) and the rejected
alternatives are in decision 0004.

The dispatch mechanism (`LLMBackend.supports_delegated_investigation`, an
explicit flag `Investigator` checks rather than a caught exception) is its
own decision — **0005**.

### 2.5 Tool surface

**Design A's tools** (Investigator's own loop, used with `AnthropicBackend`/
`OllamaBackend`):

- `datahub_lineage_query` — a thin relay, not a second abstraction layer:
  the model picks which underlying MCP tool to call (`search`,
  `get_lineage`, `get_entities`, `list_schema_fields`,
  `get_lineage_paths_between`, `get_dataset_queries`, `grep_documents`,
  `search_documents` — the confirmed tool surface from §0) and with what
  arguments; our loop dispatches the call to the real `mcp-server-datahub`
  process (spawned once per Investigator run via the `mcp` Python SDK's
  stdio client, the same product pattern decision 0003 already establishes)
  and returns the raw result as the next turn's tool result.
- `query_healthcare_db` — one parameter, `sql: str`, a `SELECT`-only
  statement. Enforced two ways, belt-and-suspenders: (1) a basic keyword
  check rejecting anything that isn't a `SELECT` (proportionate here — this
  is an LLM-generated query inside a trusted internal loop, not adversarial
  external input, so a simple filter is reasonable, not airtight-by-design);
  (2) the actual `sqlite3` connection is opened read-only
  (`sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)`) as the real
  enforcement layer — a mistake in the keyword filter still can't mutate
  `healthcare.db`, which matters since it's a committed file in this repo
  (`lld-sprint1.md`/decision 0002).
- `submit_finding` — the terminal action. Its input schema *is*
  `InvestigatorFinding` (§2.3). When the model calls this, the loop stops
  and uses the tool call's arguments directly — no prose-JSON parsing,
  because Anthropic's tool-use API guarantees the arguments match the
  declared schema. This is the concrete advantage over Design B named in
  decision 0004's "known accepted risk" section.

**Design B's tools** (the `claude -p` subprocess, used for `ClaudeCodeBackend`):
the DataHub MCP server (attached via `--mcp-config`) plus Claude Code's own
built-in `Bash` tool, scoped via `--allowedTools` to a single pinned command
shape: `sqlite3 -readonly src/datahub/healthcare.db "<query>"` — read-only
enforced at the `sqlite3` CLI level (confirmed this session:
`sqlite3 -help` lists `-readonly`), the same enforcement principle as Design
A's read-only connection, just expressed as a CLI flag instead of a
connection-string parameter. No `submit_finding` equivalent exists for
Design B — the task prompt asks the model to emit `InvestigatorFinding` as a
JSON blob in its final answer, which Investigator's own code then parses as
a second, inner JSON document (the "known accepted risk" from decision
0004).

### 2.6 Design B's concrete invocation shape

```
claude -p "<structured investigation task prompt — includes the
            SentinelFinding, the schema/lineage-confirmation instruction,
            the hypothesis-testing steps from §2.2, and an instruction to
            emit InvestigatorFinding as a fenced JSON block as the final
            answer>" \
  --mcp-config src/agents/investigator_mcp_config.json \
  --strict-mcp-config \
  --output-format json \
  --permission-mode bypassPermissions \
  --allowedTools "mcp__datahub__search,mcp__datahub__get_lineage,\
mcp__datahub__get_entities,mcp__datahub__list_schema_fields,\
mcp__datahub__get_lineage_paths_between,mcp__datahub__grep_documents,\
mcp__datahub__search_documents,\
Bash(sqlite3 -readonly src/datahub/healthcare.db *)" \
  --max-budget-usd <INVESTIGATOR_MAX_BUDGET_USD>
```

**`investigator_mcp_config.json`** is the same shape as the repo's own
`.mcp.json` — `${DATAHUB_GMS_URL}`/`${DATAHUB_GMS_TOKEN}` expansion, no
literal secrets — verified this session to expand correctly against
whichever process spawns `claude -p` (§0). It's a small, static, **committed**
file (secrets stay in `.env`, loaded by the Python process that shells out
to `claude -p`, same as every other DataHub credential in this project).

**`--strict-mcp-config`**: use *only* the DataHub server passed explicitly,
ignore this dev repo's own project-scope `.mcp.json` and any user-scope
config — the subprocess shouldn't accidentally inherit tools meant for
interactive development sessions.

**`--permission-mode bypassPermissions`**: there is no human present to
answer an interactive permission prompt for a spawned subprocess — some
non-interactive mode is required, and `bypassPermissions` is the one that
skips prompts entirely rather than auto-approving a specific category (like
`acceptEdits`, which is about file edits Investigator never makes). The
actual safety boundary is `--allowedTools` — bypassing the *prompt* is safe
specifically because the *allow-list* is narrow (read-only MCP tools, one
pinned read-only `sqlite3` command shape, nothing else — no `Write`, no
`Edit`, no unscoped `Bash`).

**Naming caveat, stated honestly**: the `mcp__datahub__<tool>` prefix above
follows Claude Code's documented `mcp__<server>__<tool>` convention for
`--allowedTools` entries sourced from an MCP server. This session verified
that `--mcp-config`'s env-var expansion works and that the fixed-cost
overhead is real (§0) — it did **not** independently re-verify the exact
`--allowedTools` string format against a real MCP tool call (the smoke test
used a fake server with no real tools to permit). Flagged as an open
verification item for whoever implements this, resolvable cheaply with one
real `claude -p` call against the actual `datahub` MCP config before relying
on it — the same kind of "verify, don't assume" step this LLD and
`lld-sprint1.md` both model throughout, not a corner cut here.

**Design A's auth path, for comparison — no new mechanism needed**: the `mcp`
Python SDK's stdio client spawns `mcp-server-datahub` directly from Python,
with an explicit `env=` dict built from `os.environ` merged with
`DATAHUB_GMS_URL`/`DATAHUB_GMS_TOKEN` loaded via `python-dotenv`'s
`load_dotenv()` — the exact same pattern already proven in
`register_ml_model.py`/`add_lineage.py` this session, just consumed by
Investigator's Python code instead of a one-time setup script.

## 3. LLM backend pluggability

`src/agents/llm_backend.py` — interface sketch only, three backends, selected
via `.env`'s `LLM_BACKEND=claude_code|anthropic|ollama` (default
`claude_code`).

```
class LLMBackend (interface):
    name: str                                     # "claude_code" | "anthropic" | "ollama"
    supports_delegated_investigation: bool          # decision 0005

    def complete(messages, tools=None, max_tokens=...) -> CompletionResult:
        """One request/response turn. Used by Design A's loop and by
        Sentinel's optional narration call (no tools attached)."""

    def investigate(task_prompt, mcp_config_path, allowed_tools,
                     max_budget_usd, timeout_s) -> InvestigationResult:
        """Delegate a full multi-turn investigation to the backend's own
        agent harness. Only meaningful where supports_delegated_investigation
        is True."""


class ClaudeCodeBackend(LLMBackend):
    name = "claude_code"
    supports_delegated_investigation = True
    # complete(): thin wrapper, `claude -p "<prompt>" --output-format json`,
    #   no --mcp-config, no tools — used only for optional narration (§1.4).
    # investigate(): §2.6's real invocation — Design B.

class AnthropicBackend(LLMBackend):
    name = "anthropic"
    supports_delegated_investigation = False
    # complete(): anthropic.Anthropic().messages.create(...), direct API call.
    # investigate(): not implemented — Investigator never calls it (§2.4/0005).

class OllamaBackend(LLMBackend):
    name = "ollama"
    supports_delegated_investigation = False
    # complete(): STUB this sprint. Interface present and typed so the
    #   3-backend contract is real, but raises a clear "not yet wired up"
    #   error rather than attempting a call — ollama isn't installed on this
    #   machine (§0), and no working local-model integration is being built
    #   this sprint, per the given scope.
    # investigate(): not implemented, same reasoning as AnthropicBackend.


def get_backend(name: str | None = None) -> LLMBackend:
    """Factory. Reads LLM_BACKEND from .env if name is None. Raises a clear,
    actionable error for an unrecognized value — never silently falls back
    to a default that isn't what was configured."""
```

**`ClaudeCodeBackend`'s two trade-offs, stated concretely (not just
asserted):**

- **Latency**: every `complete()` or `investigate()` call is a fresh
  subprocess spawn plus full CLI startup — measured this session at
  `duration_ms: 2167` for a call that did *zero* real work. A direct
  `AnthropicBackend` API call has no such floor; it's bounded by network +
  model latency alone. This is the mechanical reason §2.4 recommends
  `investigate()` be called **once** per investigation (Design B), not
  per-turn (which would multiply that floor by turn count).
- **Shared quota**: `ClaudeCodeBackend` calls consume the *same* Claude Pro
  subscription quota the repo owner uses for their own interactive Claude
  Code sessions (including sessions like the one that built this LLD).
  Running Investigator eats into the same budget as talking to Claude Code
  directly — this is not a separate, dedicated allocation. Worth knowing
  before defaulting to `claude_code` as the backend for frequent or
  automated runs; `AnthropicBackend` (a separate, metered API key) is the
  backend to reach for if that shared-quota coupling becomes a problem.

## 4. Orchestrator

### 4.1 Pipeline: Sentinel → Investigator (explicitly a subset, not a redesign)

Sequential, two stages, this sprint. This is a **subset** of `hld.md` §2.4's
eventual four-stage pipeline (Sentinel → Investigator → Remediator → Scribe)
— Remediator (US-3) and Scribe (US-4/US-5) are separate, later sprints per
`requirements.md`'s P0/P1 split. The `Incident` contract below (§4.2) is
built so those two stages append their own sections to an existing record
later, rather than requiring this sprint's contract to be reopened.

### 4.2 `Incident` — the structured findings object

```
Incident
  incident_id            string   # "INC-<YYYYMMDDTHHMMSSZ>-<provider-slug>-<condition-slug>"
                                   # timestamped to the second (not just the
                                   # date) so re-running the same segment
                                   # twice in one day doesn't collide —
                                   # same "derived, traceable, not a bare
                                   # UUID" philosophy as claim_id
                                   # (lld-sprint1.md §1.1), extended with
                                   # enough precision to actually be unique.
  created_at              string   # ISO 8601
  status                  string   # "no_anomaly" | "investigated" | "inconclusive"
                                   # ("investigated" = Investigator reached a
                                   # confident finding; "inconclusive" = it
                                   # ran but couldn't settle, mirroring
                                   # investigator.confidence/primary_root_cause
                                   # in §2.3 at the whole-Incident level so a
                                   # reader can tell the two apart without
                                   # opening the nested investigator object)
  pipeline_stages_run     [string] # ["sentinel"] or ["sentinel", "investigator"] —
                                   # explicit record of what actually ran,
                                   # so Remediator/Scribe (later) can check
                                   # this instead of assuming both stages ran
  sentinel                 SentinelFinding    # §1.3, always present
  investigator              InvestigatorFinding | null   # §2.3, null if status == "no_anomaly"
  cost
    sentinel_llm_calls        int
    investigator_turns_or_calls int
    investigator_cost_usd       float | null   # known for claude_code/anthropic; null for ollama (no metered API cost)
    wall_clock_seconds            float
```

**Why `investigator` can be `null` rather than a placeholder object**:
Sentinel's clean-run case (§6, failure mode 4) is a valid, expected outcome,
not a degenerate case of "investigation" — there's nothing to investigate,
so there's nothing to force into the shape of an investigation result.

**When is an `Incident` written to disk?** Only when `status != "no_anomaly"`
— i.e., only when Sentinel actually flagged something. A clean run prints a
CLI summary (§4.4) but writes no file: `examples/<incident-id>/` is named
for incidents, and a routine "nothing happened" run isn't one. Written as
`examples/<incident-id>/incident.json` — this is deliberately the exact
location `hld.md` §2.5 already designated for per-incident output, and is
what a later Remediator/Scribe sprint reads as input, closing that loop
without this contract needing to change shape when they're added.

### 4.3 CLI entrypoint: `guardian run`

No packaging exists in this repo yet (§0). Recommendation: add a minimal
`pyproject.toml` at the repo root with a `[project.scripts]` entry —

```
[project.scripts]
guardian = "agents.cli:main"
```

— installed once via `pip install -e .`, giving a real `guardian` command on
`PATH` that literally matches the deliverable ("CLI entrypoint `guardian
run`"), not `python -m src.agents.cli run` or a `bin/guardian` shell shim
typed out every time. This is the standard, idiomatic way Python projects
expose a command — a few lines, not new infrastructure, and a genuinely
useful thing for the repo owner to see once as a working example of how
`pip install -e .` + `[project.scripts]` turns a Python function into a
shell command.

**Rejected: a `bin/guardian` shell shim** (`#!/usr/bin/env bash exec python3
-m src.agents.cli "$@"`). Works, but isn't on `PATH` by default either
(still needs `./bin/guardian` or a manual `PATH` edit) and teaches a
project-specific convention instead of the standard Python packaging
mechanism — worse for a learner and no simpler to build.

| Flag | Default | Purpose |
|---|---|---|
| (none) | — | run Sentinel across all 30 segments; if any flagged, run Investigator on each sequentially |
| `--segment "PROVIDER,CONDITION"` | — | force-run Investigator against a specific segment regardless of Sentinel's flag — a manual override, useful for testing/demoing without waiting on the threshold |
| `--dry-run` | off | run Sentinel only, print what *would* happen, spend zero LLM budget — useful given §7's cost guardrails |
| `--llm-backend` | `.env`'s `LLM_BACKEND` | override for a single run |
| `--max-budget-usd` | `.env`'s `INVESTIGATOR_MAX_BUDGET_USD` | override for a single run, passed through to Design B's `--max-budget-usd` or enforced by Design A's own running total (§7) |

**Exit codes**: `0` for a completed run, regardless of whether an anomaly
was found — "ran successfully" and "found an incident" are different
questions, and conflating them would make a clean run look like a failure to
anything scripting around this CLI. Non-zero only for genuine operational
failures (§6).

**Multiple flagged segments** (won't happen with this sprint's seeded data —
one segment scores z=38, the rest top out at 0.64 — but the design doesn't
assume "at most one"): processed sequentially, one `Incident` per segment.
Sequential, not parallel, for the same failure-isolation reasoning decision
0004 already establishes for Investigator's own loop — a stuck or failed
investigation on segment A shouldn't complicate diagnosing segment B.

### 4.4 Run summary output

Illustrative shape (not code), printed at the end of a run that found an
incident:

```
Guardian run complete — INC-20260723T220145Z-unitedhealthcare-diabetes

Sentinel:
  Segment: UnitedHealthcare / diabetes
  Denial rate: 20.5% (371/1806) vs. 3.2% baseline (z = 38.00, threshold 3.5)
  → FLAGGED

Investigator:
  Root cause: introduced_at:claims (325/361 flagged claims, 90.0%)
  Also found: 36/361 (10.0%) inherited from mart_billing — unrelated
              baseline defect, not part of this incident
  Affected branch: claims  (mart_billing, mart_demographics, staging_patients,
                             raw_patients — checked, clean, no action needed)
  Confidence: high
  Backend: claude_code

Cost: $0.87  |  Wall clock: 41s
Written: examples/INC-20260723T220145Z-unitedhealthcare-diabetes/incident.json
```

## 5. Agent loop limits

**Design A (`AnthropicBackend`/`OllamaBackend`) — `MAX_TURNS`, primary
guardrail.** Estimated from the actual step sequence in §2.2: schema-confirm
(1-2 turns) → lineage-walk (1 turn) → segment/reason-code breakdown query
(1 turn) → immediate-upstream comparison query (1 turn) → possibly one more
hop if the first doesn't fully resolve (1 turn) → `submit_finding` (1 turn)
— roughly 6-7 turns for the happy path. **`MAX_TURNS = 12`** as a config
default: enough headroom for a retry or an extra clarifying query, hard
enough to bound worst-case cost predictably. A secondary running-token-cost
counter (using the Anthropic SDK's response `usage` fields, summed per turn)
enforces `INVESTIGATOR_MAX_BUDGET_USD` for this path too — turns and dollars
are two different, both-necessary limits (a model could in principle use few
turns with enormous responses, or many turns with small ones).

**Design B (`ClaudeCodeBackend`) — no turn cap exists to set.** §0 confirmed
no `--max-turns` flag in this CLI version's `-p` mode. `--max-budget-usd`
(§7) is the only *hard*, automatic guardrail this path has. A wall-clock
`subprocess.run(..., timeout=N)` around the call (e.g. 3-5 minutes) is a
second, backend-agnostic backstop — catches a hung/unresponsive call (e.g.
the MCP server accepted the connection but never replies) that isn't
actually spending money, which `--max-budget-usd` alone wouldn't catch.
**This asymmetry — Design A gets a real turn cap, Design B doesn't — is a
named limitation of Design B, not silently glossed over.**

**Per-turn token budget (Design A only)**: `max_tokens=4096` per `complete()`
call as a starting default — ample for tool-call-shaped responses (MCP
results, a handful of SQL rows, reasoning text), not deeply tuned, easy to
raise if truncation shows up in practice.

## 6. Failure modes

Named individually, each with the intended behavior — not a generic
"handle errors" note.

1. **MCP server down / unreachable** (subprocess fails to start, or
   `initialize` handshake times out). Investigator cannot verify lineage
   live. Intended behavior: fail that specific step clearly (not a raw
   stack trace), record it as an `evidence` entry
   ("DataHub MCP server unreachable — lineage could not be verified live"),
   and resolve to `primary_root_cause: "inconclusive"` / `confidence: "low"` — not a
   silent fallback to an assumed/hardcoded schema (that would violate
   `CLAUDE.md`'s core rule) and not a crash that kills the whole run.
   Sentinel's already-computed finding is still valid and still gets
   recorded even if Investigator can't run at all.
2. **`claude` CLI not installed** (only relevant when `LLM_BACKEND=
   claude_code`). `ClaudeCodeBackend`'s construction should check
   `shutil.which("claude")` before the pipeline starts — fail fast, at
   startup, with an actionable message pointing at falling back to
   `LLM_BACKEND=anthropic` if an API key is configured — not discovered
   mid-run after Sentinel has already done real work.
3. **Subscription limit hit mid-run** (Claude Pro cap reached partway
   through, `ClaudeCodeBackend` only). `claude -p` returns a distinctly
   shaped error result rather than the expected investigation output — this
   session's own smoke test confirmed the concrete shape:
   `"is_error": true`, `"terminal_reason": "budget_exhausted"`,
   `"subtype": "error_max_budget_usd"`. Intended behavior: detect this
   specific shape and record `primary_root_cause: "inconclusive"` with a *specific*
   reason (`"llm_backend_unavailable: subscription_limit"`), distinct from
   "the model genuinely couldn't find a confident answer" — the fix for a
   quota error (wait, or switch `LLM_BACKEND`) is completely different from
   the fix for real ambiguity, and conflating them in the record would
   mislead whoever reads it later.
4. **No anomaly found — Sentinel's clean-run case.** Not a failure. Expected,
   valid outcome. Orchestrator exits 0, Investigator never runs (saves
   cost — see §7), `Incident.status = "no_anomaly"`, no file written (§4.2),
   CLI prints a plain "no anomaly detected this run" line. Named explicitly
   so nothing downstream mistakes "nothing flagged" for a bug to retry.
5. **Investigation inconclusive** (Investigator ran, evidence doesn't
   cleanly settle — e.g. reproduction rate at some hop lands neither near 0%
   nor near 100%, per §2.2 step 7). Investigator returns a structured
   `primary_root_cause: "inconclusive"` result with `confidence: "low"` and every
   check actually made preserved in `evidence`, rather than forcing a
   confident-sounding guess. A valid, honest completion — not a crash, and
   distinct in the run summary (§4.4) from a confident finding, so a human
   can see at a glance which incidents need follow-up.

## 7. Cost guardrails

- **Sentinel**: zero required LLM cost (§1.4) — the detection math is pure
  computation. The optional narration call, if `SENTINEL_NARRATION_ENABLED`
  is set, is a single cheap call per *flagged* segment only (never per all
  30), bounding it regardless of dataset size.
- **Investigator, Design B (`claude_code`)**: `--max-budget-usd`, a real,
  confirmed-working CLI flag, is the primary guardrail — set via
  `.env`'s `INVESTIGATOR_MAX_BUDGET_USD` (a starting default around
  $0.50-$1.00 per investigation is a reasonable place to begin tuning from,
  *not* a precisely derived number — informed by this session's own finding
  that a zero-tool-call smoke test alone cost $0.11, so real investigation
  work with several MCP/Bash tool calls plausibly needs a few times that).
  Budget exhaustion produces the exact error shape named in failure mode 3
  above — handled as inconclusive, not a crash.
- **Investigator, Design A (`anthropic`/`ollama`)**: no external budget flag
  exists for a raw API — Orchestrator's own loop tracks cumulative cost from
  the Anthropic SDK's per-response `usage` fields and aborts (same
  "inconclusive, reason: budget_exceeded" outcome) once the same
  `.env`-configured, backend-agnostic `INVESTIGATOR_MAX_BUDGET_USD` cap is
  crossed. Real implementation work for whoever builds `llm_backend.py`'s
  agent loop — specified here as a requirement, not built yet.
- **Wall-clock timeout**, both designs: a blunt, simple, backend-agnostic
  backstop (§5) independent of cost/turn caps — catches a hang that isn't
  actually spending money.
- **No retry-with-model-escalation this sprint.** `hld.md` §2.4 sketches a
  future "retry twice, then escalate to Opus" behavior and explicitly calls
  it "a Sprint 2+ concern, not Sprint 1." Now that this *is* Sprint 2, it's
  still deferred, one sprint further — Investigator makes **one** attempt
  per flagged segment; an inconclusive result (failure mode 5) is reported
  for a human (or a future sprint's escalation logic) to act on, rather than
  silently auto-retrying on a more expensive model. Keeps this sprint's
  failure handling observable and debuggable first; auto-escalation is a
  refinement layered on top later, not a prerequisite for the P0 happy path.

## 8. Where things live

```
pyproject.toml                       # NEW (repo root) — [project.scripts] guardian = "agents.cli:main" (§4.3)

src/agents/
  __init__.py                         # NEW
  llm_backend.py                       # NEW — LLMBackend interface + 3 backends (§3)
  sentinel.py                           # NEW — segment stats, z-test, SentinelFinding (§1)
  investigator.py                        # NEW — Design A loop + Design B invocation, InvestigatorFinding (§2)
  investigator_tools.py                   # NEW — DataHub MCP relay + read-only healthcare.db query + submit_finding tool schema, shared by Design A's loop (§2.5)
  investigator_mcp_config.json             # NEW — committed, ${VAR}-expansion MCP config for Design B's --mcp-config (§2.6), same shape as repo-root .mcp.json
  orchestrator.py                           # NEW — Incident contract, Sentinel → Investigator pipeline, run summary (§4)
  cli.py                                     # NEW — `guardian run` argument parsing, calls orchestrator.py (§4.3)

src/codegen/                            # untouched this sprint — Remediator's domain, later sprint

examples/
  <incident-id>/
    incident.json                        # NEW per flagged incident — serialized Incident record (§4.2), future Remediator/Scribe input
```

## 9. What this sprint does NOT decide / NOT build

- **No scheduler.** `guardian run` is a single, manually (or CI-)triggered
  pass — no cron, no continuous/daemon monitoring loop.
- **No dashboard.** Already a P3 non-goal per `hld.md` §5; restated here so
  it isn't accidentally assumed to be part of the CLI run summary (§4.4).
- **No retry queues.** No durable retry/backoff infrastructure for failed
  investigations — a failure this sprint is reported (§6), not silently
  requeued.
- **No auto-retry-with-Opus-escalation.** Deferred one sprint further than
  `hld.md` §2.4 originally sketched — see §7's reasoning.
- **No parallel investigation of multiple flagged segments.** Sequential
  only, for failure isolation (§4.3).
- **No persistent run history/index beyond individual `incident.json`
  files.** No queryable store of "all incidents ever found" this sprint —
  each run's output is a standalone file.
- **No DataHub writeback of any kind.** Investigator only reads. Tags,
  assertions, and doc notes on affected datasets are Scribe's job (US-4), a
  later sprint — this sprint's findings live in `examples/<incident-id>/
  incident.json` and the CLI output only.
- **No Remediator, no generated fix code, no PR flow** (US-3, later sprint) —
  `src/codegen/` stays untouched.
- **No audit report generation** (US-5, later sprint).
- **No drift-check logic** (US-6, P2, later) — the `MLModel`/
  `MLFeatureTable`/`denial_model_scores` substrate already exists from
  Sprint 1, but nothing in this sprint reads it for drift purposes.
- **`OllamaBackend` is not functionally exercised this sprint** — interface
  present and typed (§3), consistent with the given scope; `ollama` isn't
  even installed on this machine (§0).
- **No multi-run session persistence.** Each `guardian run` invocation is an
  independent pipeline pass — no resuming a partial investigation from a
  previous run.
- **No access control on the CLI itself** — single local user, matching the
  project's overall trust model so far.
- **The exact `--allowedTools` string format for MCP-sourced tools in
  Design B (§2.6) is not independently re-verified against a real tool
  call this session** — flagged as an open, cheap-to-resolve verification
  item for implementation, not assumed correct. (Formalized as an explicit
  pre-implementation checklist item in §10.9.)

## 10. Addendum (2026-07-24) — a second seeded scenario: genuinely upstream root cause

Status: Accepted amendment. Appended after §0-§9 above were already
committed (commit `a188ba7`) and reviewed — §0-§9 are left unchanged as an
accurate record of the single-scenario design as it stood when written,
matching the "append, don't rewrite history" convention decision 0003
already used for its own resolved-follow-up note. This section is
additive: the original scenario (`UnitedHealthcare`/`diabetes`, direct
injection into `claims`) is kept exactly as designed in §0-§2, unchanged.
Full reasoning for *why* a second scenario and *why this specific
mechanism* is recorded in **decision 0006** — this section is the concrete
design (mechanism, numbers, contract changes); 0006 is the "why."

### 10.1 The gap this closes

§0-§2 designed Investigator around one seeded incident, and that incident's
evidence-backed conclusion is "not inherited — introduced at `claims`."
That's a real, correct finding, but it only demonstrates one *direction* of
Investigator's hypothesis-testing capability (§2.2 step 4: "does this
reproduce at the immediate upstream source?"). US-2's lineage-walk story and
US-3's (Remediator, later sprint) fix-generation story both need at least
one incident where the answer to that same question is genuinely "yes, all
the way to `raw_patients`" — where `mart_billing`/`staging_patients`/
`raw_patients` actually need remediation, not just `claims`. Without a
second scenario, Investigator's design can't be shown to *discriminate*
between the two answers — it could always report "introduced downstream"
and every demo run would look identical from a reader's perspective. Two
contrasting incidents, side by side, is what actually exercises "stop at
the first hop where it stops reproducing" in both directions.

### 10.2 Mechanism: why all three of raw_patients / staging_patients / mart_billing, not just mart_billing

Re-read `create_db.py` to ground this rather than assuming (per this
document's own established standard). The relevant transformation chain,
table by table:

- `staging_patients` is built as `CREATE TABLE staging_patients AS SELECT *,
  <_clean columns> FROM raw_patients` — a straight, unfiltered passthrough.
  `staging_patients.billing_amount` is *identical* to
  `raw_patients.billing_amount` for the same logical row (no cleaning
  touches this column at all).
- `mart_billing` is built as `CREATE TABLE mart_billing AS SELECT ...,
  CAST(billing_amount AS REAL) AS billing_amount, ... FROM staging_patients`
  — a type cast, sign and magnitude preserved, still no filtering.
- `claims` (`schema_sprint1.sql`) is `INSERT INTO claims ... SELECT
  b.billing_amount, ... FROM mart_billing b JOIN mart_demographics d ...` —
  another straight copy.

So the whole chain from `raw_patients.billing_amount` to
`claims.billing_amount` is copy → cast → copy, with zero cleaning logic
anywhere that would filter out a negative value. That matters for row
alignment too: **verified this session** (read-only query against the real,
committed `healthcare.db` — no mutation) that `raw_patients.rowid`,
`staging_patients.rowid`, and `mart_billing.rowid` all refer to the *same
logical row* for all 55,500 rows, at every hop:

| Join | Rows checked | Matches |
|---|---|---|
| `raw_patients.rowid = staging_patients.rowid` (compare `billing_amount`) | 55,500 | 55,500 |
| `staging_patients.rowid = mart_billing.rowid` (compare `billing_amount`) | 55,500 | 55,500 |
| `raw_patients.rowid = mart_billing.rowid` (compare `billing_amount`, direct) | 55,500 | 55,500 |

This holds because none of the three `CREATE TABLE ... AS SELECT`
statements that build `staging_patients`, `mart_billing`, or
`mart_demographics` use a `WHERE` or `ORDER BY` — a plain full-table scan
preserves source row order, so the freshly assigned rowids in each new
table land in the same sequence as the table they're built from. (This is
the same fact `lld-sprint1.md` §0 already relied on for `mart_billing.rowid
= mart_demographics.rowid`; this session extends the same verification one
hop further back, to `raw_patients`.)

**Given that, injecting into `mart_billing` alone would create a different,
ambiguous third kind of finding** — a defect real at the `mart_billing` hop
but absent from `staging_patients`/`raw_patients`. Investigator's hop-by-hop
check (§2.2 step 4) would correctly report that as
`"introduced_at:mart_billing"`, which is a real, valid finding *in
general*, but it is not the "genuinely upstream, traceable all the way to
`raw_patients`" contrast case the amendment asks for. **Decision: the
injection writes the same negative value, at the same rowid, into all
three of `raw_patients`, `staging_patients`, and `mart_billing`.** This is
what makes the defect reproduce cleanly at every hop and gives an
unambiguous `"inherited_from:raw_patients"` conclusion — matching US-2's
literal "claims ← mart_billing ← staging ← raw" chain exactly, which is
precisely the contrast case being asked for.

**On the reason code, confirmed explicitly rather than left implicit**: no
new `denial_reason_code` enum value is needed. `generate_denials.py`'s
existing rule 1 (`deny_negative_billing()`) denies *any* claim with
`billing_amount < 0` as `INVALID_BILLING_AMOUNT`, regardless of where that
negative value originated — it doesn't inspect lineage, just the value.
Once the upstream injection makes `mart_billing.billing_amount` negative
for the target rows, `schema_sprint1.sql`'s unmodified `INSERT INTO claims
... SELECT b.billing_amount ...` carries that negative value into `claims`
automatically, and rule 1 denies it automatically, with the same reason
code the existing scenario already uses. **No changes needed to
`schema_sprint1.sql`, `generate_denials.py`'s rule 1, or the reason-code
enum** — this is the concrete payoff of injecting upstream rather than
hand-rolling a second, parallel denial path: the new scenario reuses 100%
of the existing, already-verified machinery downstream of the injection
point.

### 10.3 Chosen segment, target rate, seed

| Parameter | Existing scenario (unchanged) | New scenario |
|---|---|---|
| Segment | `UnitedHealthcare` / `diabetes` | `Cigna` / `obesity` |
| Injection point | `claims.billing_amount` directly (post-population) | `raw_patients` + `staging_patients` + `mart_billing` (pre-population) |
| Random seed | `RANDOM_SEED = 42` | `UPSTREAM_SEED = 43` |
| Target rate | `SPIKE_TARGET_RATE = 0.20` (20%) | `UPSTREAM_TARGET_RATE = 0.15` (15%) |

**Why a different provider *and* a different condition, not just a
different provider**: the existing scenario is
`UnitedHealthcare`/`diabetes`. Reusing `diabetes` under a different
provider (or `UnitedHealthcare` under a different condition) risks reading,
in a live demo, as "diabetes claims are systematically bad" or
"UnitedHealthcare claims are systematically bad" rather than two genuinely
distinct, unrelated incidents — weakening exactly the "Investigator
discriminates between origins" story this amendment exists to tell.
`Cigna`/`obesity` shares neither dimension with `UnitedHealthcare`/`diabetes`.

**Why `UPSTREAM_SEED = 43`, not reusing `42`**: a distinct seed, not a
copy, so the two scenarios' random row selections are independent of each
other (reusing `42` wouldn't cause a bug, since the two `rng.sample()`
calls draw from different populations — but a visibly different, explicitly
offset value states "this is a separate, deliberately chosen parameter"
rather than looking like an accidental copy-paste, the same reasoning
`generate_denials.py` already applies by naming its tunables explicitly
rather than burying them).

**Why `15%`, not reusing `20%`**: distinct from the existing scenario's
rate for the same "deliberate, not copy-pasted" reason, while still large
enough (≈4x the composed-dataset baseline, §10.5) to produce a cleanly
separated z-score — confirmed below, not assumed.

### 10.4 Simulation methodology — how this was verified without touching `healthcare.db`

Per the hard constraint on this task, the committed `src/datahub/healthcare.db`
was never mutated. Verification method: copied the real file to an isolated
scratchpad directory (outside the repo), copied the *actual, unmodified*
`schema_sprint1.sql`/`generate_denials.py`/`score_claims.py` alongside it,
wrote a draft injection script (`seed_upstream_scenario.py` — simulation-only,
not committed; Slice 0 will write the real version), and ran the full
sequence against the copy:

```
python seed_upstream_scenario.py             # NEW — injects into raw_patients/staging_patients/mart_billing
sqlite3 healthcare.db < schema_sprint1.sql    # unmodified — rebuilds claims from the now-modified mart_billing
python generate_denials.py                    # unmodified — both scenarios compose here
python score_claims.py                        # unmodified
```

Using the real, unmodified downstream scripts (rather than hand-simulating
what they'd do) means this isn't a hand-wave — it's the actual pipeline
logic, run against a throwaway copy. **Confirmed after the session**:
`git status --short` and `git diff --stat -- src/datahub/healthcare.db`
both produced zero output against the real repo — the tracked file is
byte-identical to before this session started.

### 10.5 Results — both scenarios' z-scores, composed

Computed with the exact two-proportion z-test from §1.2 (leave-one-out
baseline), against all 30 segments, with **both** scenarios' denials
present simultaneously (the realistic case — both incidents exist in the
same pipeline run):

| Segment | n | denied | rate | baseline (leave-one-out) | z |
|---|---|---|---|---|---|
| `UnitedHealthcare` / `diabetes` (existing) | 1,806 | 375 | 20.76% | 3.67% | **35.53** |
| `Cigna` / `obesity` (new) | 1,864 | 298 | 15.99% | 3.81% | **25.69** |
| *closest of the other 28 (untouched)* | 1,740 | 65 | 3.74% | 4.24% | −1.03 |
| *most extreme of the other 28 (untouched)* | 1,870 | 43 | 2.30% | 4.29% | −4.21 |

Both flagged segments clear `Z_THRESHOLD = 3.5` by a wide margin, and
**every one of the other 28 segments now scores *negative*** (ranging
−1.03 to −4.21) — none approach the positive threshold at all. This is an
even cleaner separation on the "will this false-positive?" question than
the single-scenario case, for a specific, checkable reason (§10.6).

**Reproduction check, confirming the new scenario reads as genuinely
upstream** (not just "flagged," but flagged *for the right, evidence-backed
reason*): of the 280 `INVALID_BILLING_AMOUNT` denials in `Cigna`/`obesity`,
**280/280 (100%)** reproduce at `mart_billing`, **280/280 (100%)** at
`staging_patients`, and **280/280 (100%)** at `raw_patients` — an
unambiguous `"inherited_from:raw_patients"` case, unlike the existing
scenario's 90%/10% split. The existing scenario's own row-level facts are,
as expected, completely unchanged by adding the second one: still exactly
361 `INVALID_BILLING_AMOUNT` denials in `UnitedHealthcare`/`diabetes`,
still exactly 36 of those reproducing in `mart_billing` — the new scenario
is additive at the data level; it only affects *aggregate* z-score
baselines (§10.6).

### 10.6 Checked, not assumed: does the second scenario perturb the first scenario's z-score?

**Yes — measurably, though not in a way that changes any conclusion.** The
original single-scenario design (§1.2) reported `z = 38.00` for
`UnitedHealthcare`/`diabetes`. With the second scenario composed into the
same dataset, it's **`z = 35.53`** — a real shift (about 6.5% relative),
not negligible in absolute terms, and worth reporting honestly rather than
waving away. The mechanism is straightforward: the z-test's leave-one-out
baseline for `UnitedHealthcare`/`diabetes` is computed from "every other
segment," which now includes `Cigna`/`obesity`'s own inflated denial count
— that raises the baseline rate being compared against (3.21% → 3.67%),
which pulls `z` down somewhat. The same effect runs in the other direction
for the 28 untouched segments: with *two* real spikes now contributing to
everyone else's "rest" pool, their baselines are pulled up too, which is
why all 28 of them now score negative rather than the single-scenario
case's small positive noise ceiling (+0.64). **Neither shift threatens the
design**: both flagged segments remain enormously separated from the
untouched segments' scores (25.69 and 35.53 vs. a −1.03-to−4.21 band), so
`Z_THRESHOLD = 3.5` still cleanly and unambiguously flags exactly the two
seeded incidents and nothing else. This interaction — multiple simultaneous
real incidents slightly shifting each other's and everyone else's
leave-one-out baseline — is a real, understandable property of this
statistical method with more than one true anomaly present, not a flaw in
this specific pair of numbers; noted here so it isn't mistaken for one if a
future sprint adds a third scenario.

### 10.7 Sequencing (design-level; the actual script is Slice 0, not written here)

The new injection **must run before `schema_sprint1.sql`** is (re-)run,
because `schema_sprint1.sql` unconditionally `DROP`s and rebuilds `claims`
from whatever is currently in `mart_billing` — if the injection happened
after, its changes to `mart_billing` would sit unused until the next
rebuild. Full order:

1. **`seed_upstream_scenario.py`** (NEW, Slice 0) — injects into
   `raw_patients`/`staging_patients`/`mart_billing` for `Cigna`/`obesity`,
   per §10.2/§10.3.
2. `sqlite3 healthcare.db < schema_sprint1.sql` (unchanged) — rebuilds
   `claims`, now inheriting the injected values automatically.
3. `python generate_denials.py` (unchanged) — runs the existing
   `seed_segment_spike()` for `UnitedHealthcare`/`diabetes` exactly as
   before, then rule 1 denies *both* segments' negative-billing claims with
   the same `INVALID_BILLING_AMOUNT` reason code.
4. `python score_claims.py` (unchanged).

This ordering, and the fact that steps 2-4 need zero code changes, is the
concrete reason §10.2 recommends the upstream-injection mechanism over any
alternative that would touch `claims` or `generate_denials.py` directly for
the new scenario too.

### 10.8 Update to Investigator's contract (§2.3)

§2.3's `InvestigatorFinding.root_cause_breakdown` example only showed a
*minority*-upstream case (325/90% `introduced_at:claims`, 36/10%
`inherited_from:mart_billing`). With this second scenario, the contract
needs to read correctly for the opposite composition too — confirmed here
rather than left as an untested assumption:

- For `Cigna`/`obesity`: `primary_root_cause = "inherited_from:raw_patients"`,
  `root_cause_breakdown = [{classification: "inherited_from:raw_patients",
  claim_count: 280, pct: 100.0, note: "negative billing_amount present at
  every upstream hop — mart_billing, staging_patients, and raw_patients"}]`,
  `affected_branch = ["raw_patients", "staging_patients", "mart_billing",
  "claims"]` (the full chain — unlike the existing scenario, real
  remediation is needed at every stage here, not just `claims`),
  `datasets_checked_and_clean = []` (nothing upstream is clean in this
  case — everything checked is implicated).
- No change to the field *shapes* themselves (`primary_root_cause`,
  `root_cause_breakdown`, `affected_branch`, `datasets_checked_and_clean`
  all already existed in §2.3) — only the *example values* needed
  extending, since the original example happened to only exercise the
  minority-upstream branch. §2.3's table is otherwise unchanged; this
  addendum's job was to confirm the existing contract shape actually covers
  a majority/100%-upstream case cleanly, and it does, with no schema
  changes.
- One genuinely new implication for Investigator's hypothesis-testing
  workflow (§2.2 step 4), worth stating explicitly: the "stop at the first
  hop where it stops reproducing" logic needs to keep walking *all the way
  to `raw_patients`* when the anomaly never stops reproducing, not just
  check one hop and assume "upstream" if it reproduces once. §2.2's step 4
  already says "continue one hop further and repeat" for the
  reproduces-case — this scenario is the first concrete evidence that
  branch of the logic is actually necessary (the existing scenario alone
  never exercised it, since its anomaly stopped reproducing at the very
  first hop checked).

### 10.9 Pre-implementation checklist — before Slice 3 (Investigator)

**MUST verify before implementing Investigator's Design B path (§2.6):**
the exact `--allowedTools` string syntax for MCP-sourced tools
(`mcp__datahub__<tool>`, per Claude Code's documented convention) has
**not** been independently confirmed against a real `claude -p` tool call —
the smoke test that verified `--mcp-config`'s env-var expansion (§0) used a
fake server with no real tools to permit, so it could not exercise this
specific string format. Already flagged as an open item in §9; formalized
here as an explicit, actionable gate:

- [ ] **Before Slice 3 (Investigator implementation) begins**: run one real
      `claude -p` call against the actual `datahub` MCP config
      (`investigator_mcp_config.json`, once it exists) with `--allowedTools`
      set to a single real DataHub tool name (e.g. `mcp__datahub__search`),
      confirm the call actually reaches and uses that tool (not silently
      falling through to "tool not permitted" or a permission prompt), and
      only then proceed to wire up the full tool list in §2.6's invocation
      shape. Cheap (one call, one tool), and resolves a genuinely
      load-bearing unknown before it's built into the real implementation.

### 10.10 What this addendum does NOT decide / NOT build

- **The actual `seed_upstream_scenario.py` script is Slice 0, future
  implementation work** — not written by this addendum.
  `seed_upstream_scenario.py` as used in §10.3/§10.4 is a simulation-only
  draft, run against a throwaway scratch copy of `healthcare.db`, never
  committed, and deleted at the end of this session.
- **No changes to `schema_sprint1.sql`, `generate_denials.py`,
  `score_claims.py`, or the `denial_reason_code` enum** — confirmed
  unnecessary (§10.2), not just assumed.
- **No third scenario.** Two is what the amendment asked for; §10.6's noted
  interaction effect (baselines shifting as more real incidents are added)
  is worth knowing about *if* a third is ever added later, not a reason to
  add one now.
- **No change to Sentinel's `Z_THRESHOLD` default (still 3.5, §1.2)** — both
  scenarios clear it with large margins under the composed-dataset numbers
  in §10.5, so no retuning is needed.
