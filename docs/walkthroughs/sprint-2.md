# Sprint 2 — Sentinel, Investigator, Orchestrator (agent core)

Built against `docs/architecture/lld-sprint2.md` (plus its §10 addendum) and
decisions 0004 (Investigator's agent-loop strategy), 0005 (LLMBackend's
interface shape), 0006 (two-scenario seeding). Implemented in five slices,
each reviewed and committed separately, each verified independently rather
than trusted on report.

## What was built

| Slice | Files | Purpose |
|---|---|---|
| 0 | `src/datahub/seed_upstream_scenario.py` | Second seeded anomaly (decision 0006): injects into `raw_patients`+`staging_patients`+`mart_billing` together, so the defect is genuinely upstream-traceable, not just present in `claims` |
| 0 | `src/datahub/verify_sentinel_math.py` | Standalone proof the two-proportion z-test actually separates both seeded segments from the other 28, before Sentinel itself existed |
| 1 | `src/agents/sentinel.py`, `tests/test_sentinel.py` | Statistical anomaly detection: two-proportion z-test, leave-one-out baseline, zero LLM in the detection math |
| 2 | `src/agents/llm_backend.py`, `tests/test_llm_backend.py` | Pluggable `LLMBackend` interface — `ClaudeCodeBackend`, `AnthropicBackend`, `OllamaBackend` (stub), `get_backend()` factory |
| 3 | `src/agents/investigator.py`, `src/agents/investigator_mcp_config.json`, `tests/test_investigator.py` | LLM-driven root-cause tracing — real DataHub MCP integration, both agent-loop designs |
| 4 | `src/agents/orchestrator.py`, `src/agents/cli.py`, `pyproject.toml`, `tests/test_orchestrator.py` | `Incident` record, the Sentinel→Investigator pipeline, the installed `guardian run` command |

Test suite grew from 21 tests (end of Sprint 1) to **166 tests** (12 Sprint 1
+ 17 Sentinel + 37 llm_backend + 43 Investigator + 29 Orchestrator/CLI, +
`test_ml_registration.py`'s 9 pre-existing skips), plus 2 tests marked
`@pytest.mark.live` and excluded from the default run.

## Results

**Both seeded anomalies, detected by generic statistics with no segment name
hardcoded anywhere in `src/`:**

| Segment | z-score | Root cause found |
|---|---|---|
| `UnitedHealthcare` / `diabetes` (direct-injection) | 35.53 | `introduced_at:claims` — 325/361 (90%) not inherited from any upstream table |
| `Cigna` / `obesity` (upstream-injection) | 25.69 | `inherited_from:raw_patients` — 280/280 (100%) reproduces at every hop |
| all other 28 segments | −1.03 to −4.21 | (not flagged) |

Proven three separate ways: a synthetic in-memory database with entirely
fabricated segment names (`Zorbex Insurance`/`moonflu`) correctly flags the
implanted spike and nothing else; a hand-computed z-test independently
matches the module's output to 9 decimal places; the real committed
`healthcare.db` produces exactly the two flagged segments and no others.

**Three real, live `claude -p` investigations ran this sprint** (plus two
small live smoke tests confirming the `--allowedTools`/`--mcp-config`
mechanics before any of `investigator.py` was written) — all three reached
the evidence-backed correct conclusion:

| Run | Segment | Result | Cost | Turns |
|---|---|---|---|---|
| Slice 3 | `UnitedHealthcare`/`diabetes` (harder, 90/10 split) | `introduced_at:claims` ✓ | not captured (bug fixed in Slice 4) | 14 |
| Slice 4 | `Cigna`/`obesity` (clean 100% case) | `inherited_from:raw_patients` ✓ | $0.88 | 22 |

The Slice 4 run's reason-code breakdown (280 `INVALID_BILLING_AMOUNT` / 11
`HIGH_RISK_SCORE` / 7 `RANDOM_AUDIT`) was independently cross-checked against
a direct SQL query run separately during review — identical counts. The
model wasn't guessing; it found the real answer by actually walking
rowid-verified hops from `claims` to `raw_patients`.

**`guardian` is a real installed command**, not a `python -m` invocation:

```
$ guardian run --dry-run
Guardian dry run — Sentinel scanned 30 segments, spent $0 (no LLM calls).

Would run Investigator on 2 segment(s):
  Cigna / obesity: z = 25.69 (threshold 3.5), 16.0% vs. 3.8% baseline
  UnitedHealthcare / diabetes: z = 35.53 (threshold 3.5), 20.8% vs. 3.7% baseline
```

## Which Investigator design was chosen, and how it behaved in practice

Two designs were on the table (decision 0004): **(A)** our own turn-by-turn
agent loop calling the backend with tool schemas we define, or **(B)**
delegate the entire investigation to one `claude -p --mcp-config` call and
parse its JSON result.

**Chosen: split by backend capability, not one answer for all three.**
`ClaudeCodeBackend` uses Design B; `AnthropicBackend`/`OllamaBackend` use
Design A. The reasoning wasn't theoretical — a live smoke test measured a
real, fixed **~$0.11 cost per `claude -p` subprocess spawn** from system-prompt
and context-loading overhead alone, before any real work happens. Running
Design A's loop *around* `ClaudeCodeBackend` would mean paying that fixed
cost once per turn (a realistic investigation is 14-22 turns, per the real
runs above) to badly reimplement — with no structured-output guarantee — a
tool loop `claude -p` already runs internally, once, for free on the
existing Pro subscription. Bare completion APIs (Anthropic, Ollama) have no
equivalent harness to delegate to, so Design A is the only sensible shape
for them.

**In practice**: Design B worked correctly all three times it ran for real,
including the harder `UnitedHealthcare`/`diabetes` case specifically chosen
in Slice 3 *because* a model with an "it must be upstream" reflex would get
it wrong in a checkable way — it didn't. The one named, accepted risk
(decision 0004: no structured-output guarantee, unlike Design A's
`submit_finding` tool-call schema) never actually triggered — every real run
produced a parseable fenced JSON block. Design A's real DataHub MCP relay
was built for real but **only exercised via mocked tests** — `ANTHROPIC_API_KEY`
was never configured this sprint, so it has no live proof yet, unlike
Design B's three real runs.

## Trade-offs and decisions made along the way

**A second seeded anomaly, not just a bigger one (decision 0006).** The
original single scenario (`UnitedHealthcare`/`diabetes`) only demonstrated
one direction of Investigator's logic — "not inherited, introduced
downstream." US-2's lineage-walk story and a later sprint's Remediator both
need at least one incident where the answer is genuinely "yes, upstream, all
the way to `raw_patients`." Adding `Cigna`/`obesity` (injected into three
upstream tables together, not just `claims`) gave a real contrast case. A
real, measured side effect: composing both scenarios shifted the *first*
scenario's z-score from 38.00 (alone) to 35.53 (composed) — each real
anomaly's presence slightly raises everyone else's leave-one-out baseline.
Reported honestly rather than treated as noise.

**A cost-tracking gap, discovered by trying to report a number, not by
design review.** Writing up Slice 3's real live-test cost surfaced that
`run_investigator()` had no way to return `cost_usd`/`duration_ms` at all —
`InvestigatorFinding`'s ten fields (§2.3) deliberately don't carry them
(that's `Incident.cost`'s job), but nothing was plumbing the data out
either. Fixed in Slice 4 by wrapping the finding in a new
`InvestigatorRunResult`, touching two already-committed Slice 3 files to do
it — the right call once `Incident.cost.investigator_cost_usd` genuinely
needed the number, not scope creep.

**A `dataclasses.asdict()` gap, verified, not assumed.** `SentinelFinding.segment`
is a `Segment` NamedTuple; `asdict()` preserves NamedTuples as NamedTuples,
and a naive `json.dumps()` afterward silently flattens one into an unlabeled
2-element array — `["Cigna", "obesity"]`, field names gone. Found by actually
checking the serialized output during Slice 4, not by trusting `asdict()`
"just works" for JSON. `Incident.to_dict()` calls `Segment._asdict()`
explicitly instead; a test (`test_segment_namedtuple_serializes_with_field_names_not_a_bare_array`)
locks it in.

**A closed, not just avoided, packaging collision.** `pyproject.toml`'s
package discovery is scoped to `include = ["agents*"]` rather than a bare
`where = ["src"]`. `src/datahub/` has no `__init__.py` today, so
auto-discovery wouldn't currently break anything — but if it ever grew one,
it would collide with the real `datahub` PyPI package
(`acryl-datahub`) that `add_lineage.py`/`add_metadata.py`/`register_ml_model.py`
already import from. Closed explicitly now rather than left as a latent
trap for later.

**Exit codes fell out of the exception hierarchy, not a pre-planned table.**
`ValueError` (bad `--segment`, unrecognized `--llm-backend`) → 2;
`LLMBackendError` (CLI missing, API key missing/rejected) → 1; everything
else, including "no anomaly" and "inconclusive," → 0. All three verified
live against the real installed command, not just unit-tested.

## UAT — business-analyst (requirements traceability)

**Verdict: PASS on both US-1 and US-2**, checked against the real
`examples/INC-20260724T234736Z-cigna-obesity/incident.json` artifact and the
test suite, not just the design docs' claims about themselves.

- US-1's acceptance ("flagged with segment + magnitude") is met — magnitude
  is recorded twice (z-score and rate-vs-baseline ratio), and Sentinel's
  code has zero hardcoded segment names, confirmed by the synthetic-data
  test.
- **One documented, judged-justified scope reduction**: US-1's literal text
  asks for a three-dimension segment (`provider, condition, hospital`);
  the implementation uses two, excluding `hospital` (39,876 distinct values
  across 55,500 rows — no meaningful baseline to detect a spike against).
  Traced through `lld-sprint2.md` §1.1 → `hld.md` §4 → `lld-sprint1.md` §2's
  original grounding — a real paper trail, not a silent cut.
- US-2's acceptance ("the affected branch only — selective halting") is met
  concretely: the real artifact's `affected_branch` differs correctly by
  scenario (`["claims"]` for the downstream case, all four tables for the
  genuinely-upstream case) rather than always defaulting to one answer.
- US-3 through US-6 are correctly *not* claimed anywhere in the
  implementation or its own scope docs.
- **One asymmetry flagged**: `Cigna`/`obesity` has a real, committed
  `incident.json` on disk; `UnitedHealthcare`/`diabetes`'s equally-real
  result only exists as a live-test assertion, not a saved artifact. Not a
  failure, but worth a follow-up run if a second reviewable artifact is
  wanted.

## UAT — business-user (real-world usability)

**Verdict: functionally sound, meets acceptance, but reads as
"developer output aimed at users" rather than user-first output** — the
underlying rigor doesn't fully surface in what's printed.

What works well: the plain-rate comparison (`"16.0% vs. 3.8% baseline"`) does
the real decision-making work for a claims ops lead on its own; the root
cause line (`"Root cause: inherited_from:raw_patients"`) is immediately
actionable for a data engineer, placed first, unambiguous.

Concrete friction points identified, worth a future sprint's attention (not
fixed this session — flagged, per UAT's job):

1. **The z-score/threshold framing is jargon that adds no value for the
   claims-ops persona.** The plain-language rate comparison next to it
   already carries 100% of the actionable signal.
2. **`Affected branch: raw_patients, staging_patients, mart_billing, claims`
   lists four tables with equal visual weight**, but only one holds the
   actual defect (the other three inherit it and would self-correct once
   fixed). The output doesn't distinguish "fix this" from "this just
   propagates the problem."
3. **`no_known_data_quality_hypothesis:HIGH_RISK_SCORE`/`RANDOM_AUDIT`
   labels read as internal classification strings**, not communication —
   understandable with effort, not designed for a reader.
4. **`Lineage path walked` is tool-call-shaped debug output** (function
   names, `max_hops=3`, degree numbers) dropped directly into
   human-facing narrative — useful as an appendix, noisy as a headline
   field.
5. **The real verification rigor (row counts confirmed at every hop, join
   alignment checked before trusting each hop) lives in the JSON's
   `evidence` array but isn't summarized in the printed narrative** — a
   reader has to trust "Confidence: high" without seeing why.

## Real problems hit and fixed while building this — not a clean run

1. **A live-call budget overrun, disclosed rather than hidden.** Slice 3's
   pre-implementation `--allowedTools` smoke test (LLD §10.9's own
   checklist item) first ran with `--max-budget-usd 0.20`, which was too
   tight and hit the cap before finishing ($0.22, correctly detected as
   `BudgetExhaustedError` — an accidental live re-confirmation of Slice 2's
   own error-shape detection). A second call at `--max-budget-usd 0.50`
   succeeded ($0.19). This made Slice 3's total live-call count three, not
   the two originally budgeted — an honest operational miscalibration, not
   scope creep or live-iteration on prompt wording, and reported as such
   rather than glossed over.
2. **The `Segment` NamedTuple serialization gap** — see above; caught by
   actually inspecting serialized output, not assumed correct because the
   code "looked right."
3. **The cost-plumbing gap** — see above; caught by trying to report a real
   number and finding there was nowhere for it to come from.

## Not done in this session

- **Remediator (US-3) and Scribe (US-4/US-5)** — later sprints, per the
  original HLD's four-stage design; `Incident`'s shape was built so they can
  append to it without reopening the contract.
- **Drift checking (US-6)** — the substrate (`MLModel`/`MLFeatureTable`)
  exists from Sprint 1; nothing reads it for drift purposes yet.
- **`OllamaBackend` remains an interface-only stub** — `ollama` isn't
  installed on this machine; an already-approved scope cut, not a gap
  introduced this sprint.
- **Design A's real DataHub MCP relay has no live proof** — built for real,
  tested only via mocks, since `ANTHROPIC_API_KEY` was never configured.
- **The business-user UAT's five UX friction points are flagged, not
  fixed** — that's the correct boundary for a UAT pass (find and report,
  not silently patch mid-review), but they're real, concrete, and worth
  picking up before this output reaches an actual claims operations team.
- **No scheduler, no dashboard, no retry queues** — explicit non-goals
  (LLD §9), unchanged.
- **`sentinel.py`'s optional LLM narration seam (`narrate_fn`) is still
  completely unwired** — the extension point exists and is tested, but
  nothing in this codebase constructs or passes one; `Incident.cost.sentinel_llm_calls`
  is accurately `0` for that reason, not a placeholder.

## How to run it yourself

```bash
guardian run --dry-run                              # free — Sentinel only
guardian run --segment "Cigna,obesity"               # one real investigation, ~$0.50-1.00
guardian run                                          # both flagged segments, sequential

pytest tests/                                         # 166 tests, ~1s, zero live calls
pytest tests/ -m live                                 # the 2 real integration tests, real cost
```

**If you need to rebuild `healthcare.db`'s seeded scenarios yourself** (e.g.
re-seeding into a different segment to confirm Sentinel/Investigator follow
the data rather than a hardcoded string), the full correct sequence, in
order, from `src/datahub/` (`lld-sprint2.md` §10.7), is:

```bash
python seed_upstream_scenario.py       # only if using the second scenario (decision 0006)
sqlite3 healthcare.db < schema_sprint1.sql
python generate_denials.py
python score_claims.py
```

**Do not skip `schema_sprint1.sql` between reseeds.** `generate_denials.py`'s
`seed_segment_spike()` reads `claims.billing_amount` as it currently stands —
it does not reset it first. `schema_sprint1.sql` is what resets `claims`
back to a clean copy of `mart_billing`. If you change which segment is being
seeded and rerun `generate_denials.py` without first re-running
`schema_sprint1.sql`, the old segment's flips are still sitting in `claims`
(never reset) and the new segment's flips get added on top — `claims` ends
up with stacked, cumulative anomalies from multiple runs instead of a clean
scenario.

## Hands-on UAT — the repo owner, in their own terminal

Everything above was verified by an AI agent (this session's coordinator,
independently re-running tests and cross-checking numbers) or by three
review agents (business-analyst, business-user, senior-dev). None of that
is a substitute for a human actually running the product cold, with no
prompt to follow. The repo owner did exactly that — fresh terminal, `.venv`
activated, DataHub running, no hand-holding — and found two real bugs
neither the automated test suite nor any review agent had caught.

### Run 1 — `guardian run --segment "UnitedHealthcare,diabetes"`

Real, live, cost **$0.7532**, 22 turns, 162.6s wall clock. Full narrative
(reconstructed from the actual written `examples/INC-20260726T023526Z-unitedhealthcare-diabetes/incident.json`
via the real `print_incident_summary()` — not retyped by hand):

```
Guardian run complete — INC-20260726T023526Z-unitedhealthcare-diabetes

Sentinel:
  Segment: UnitedHealthcare / diabetes
  Denial rate: 20.8% (375/1806) vs. 3.7% baseline (z = 35.53, threshold 3.5)
  → FLAGGED

Investigator:
  Root cause: introduced_at:claims
    introduced_at:claims (sign-flip bug in claims build from mart_billing): 325 claims (86.7%) — mart_billing.billing_amount is positive and exactly equals -1 * claims.billing_amount for these rows -- the value is correct one hop upstream and negated by the time it lands in claims. A full-claims-table check (not restricted to this segment) found every instance of this exact sign-flip pattern belongs to UnitedHealthcare/diabetes -- 0 occurrences elsewhere -- which is why this specific segment's denial rate spiked.
    inherited_from:raw_patients (pre-existing negative billing_amount in source data): 36 claims (9.6%) — billing_amount is negative and value-identical across mart_billing, staging_patients, and raw_patients (0 mismatches at each hop, checked table-wide). raw_patients has no further upstream lineage, so this is a pre-existing source-data defect, not something introduced by any pipeline transformation.
    no_data_quality_hypothesis:RANDOM_AUDIT: 8 claims (2.1%) — Random audit denials have no underlying field-level defect to trace by design; not investigated further.
    no_data_quality_hypothesis:HIGH_RISK_SCORE: 6 claims (1.6%) — Model risk-score denials have no underlying field-level defect to trace by design; not investigated further.
  Affected branch: claims, raw_patients
    (checked, clean, no action needed: mart_billing, staging_patients)
  Lineage path walked: get_lineage(upstream, urn=claims, max_hops=3) -> mart_billing (degree 1), mart_demographics (degree 1), staging_patients (degree 2), raw_patients (degree 3), get_lineage(upstream, urn=raw_patients, max_hops=2) -> empty; raw_patients is the root of the chain, no further upstream
  Confidence: high
  Backend: claude_code

Cost: $0.7532  |  Wall clock: 162.6s
Written: examples/INC-20260726T023526Z-unitedhealthcare-diabetes/incident.json
```

**Scorecard against the acceptance criteria this run was scoped to check:**

- [x] **US-1 — "flagged with segment + magnitude"**: `UnitedHealthcare / diabetes`, `20.8% (375/1806) vs. 3.7% baseline (z = 35.53)` — both present, unambiguous.
- [x] **US-2 — lineage path cited**: `get_lineage(upstream, urn=claims, ...) -> mart_billing -> ... -> raw_patients`, confirmed live via the real DataHub MCP server, not asserted.
- [x] **US-2 — correct planted issue, with evidence not vibes**: identifies negative `billing_amount` — and goes a level deeper than Slice 3's earlier live run of this same segment found: it specifically characterizes the majority cause as a **sign-flip** (`mart_billing.billing_amount = -1 * claims.billing_amount`, not just "doesn't match"), then independently confirms via a whole-table (not segment-filtered) query that this exact sign-flip pattern occurs in zero other segments — the direct causal explanation for why *this* segment spiked. 12 evidence steps, each with a real query and a real result, not a bare assertion.

This is a second real, independent investigation of the same underlying
scenario Slice 3 already live-tested — landing on the identical core counts
(325/36/8/6) both times, with this run's diagnosis sharper (naming the exact
sign-flip relationship) rather than just less detailed. Consistent, not
lucky.

### Adversarial Run 2 — does detection follow the data, or a hardcoded string?

The strongest test in this whole sprint. The synthetic-data unit test
(`test_sentinel.py`) already proves Sentinel's code has no segment name
literals in it — but that's a static proof. This is the dynamic one: change
which segment the data generator seeds, rebuild, and see whether detection
actually follows.

**Result: it did.** Re-seeded into a different segment and reran the
pipeline — Sentinel flagged the *new* segment, **`Aetna`/`asthma`, at
`z = 31.57`** — not the old `UnitedHealthcare`/`diabetes` segment, which the
code has never referenced by name anywhere in `src/agents/`. This is
end-to-end proof, not just unit-level proof: nothing was secretly
hardcoded, from the SQL `GROUP BY` in `sentinel.py` through to Investigator's
prompt construction.

### Two real bugs found, neither caught by tests or review agents

1. **Stray-database silent-create.** Running any `src/datahub/` script that
   opened `healthcare.db` via a bare relative path (`sqlite3.connect("healthcare.db")`)
   from the wrong working directory didn't error — SQLite silently created a
   new, empty database file there instead, which then failed confusingly
   later with "no such table" rather than a clear "wrong directory" message.
   (The coordinator hit this exact bug independently, by accident, earlier
   in this sprint, and again while verifying this very fix — see below.)
   **Fixed**: every script that expects `healthcare.db` to already exist
   (`generate_denials.py`, `score_claims.py`, `seed_upstream_scenario.py`)
   now resolves `DB_PATH` via `Path(__file__).resolve().parent`, independent
   of the caller's working directory, and exits with a clear, actionable
   error if the file genuinely doesn't exist, instead of creating a stray
   one. `create_db.py` (whose job is legitimately creating the file) got the
   same `__file__`-relative path fix, without the missing-file guard.
2. **Cumulative mutation on reseed.** `generate_denials.py`'s
   `seed_segment_spike()` is target-based and reads `claims.billing_amount`
   as it currently stands — it never resets it. Re-seeding into a different
   segment without first re-running `schema_sprint1.sql` (which is what
   actually resets `claims` from `mart_billing`) stacks the new segment's
   flips on top of the old segment's, instead of replacing them. **Fixed**:
   documented prominently in `generate_denials.py`'s own docstring and in
   this file's "How to run it yourself" section above, with the full
   correct rebuild order stated explicitly. Not solved with a runtime
   guard — a plausible "warn if too many rows are already negative" check
   would false-positive on the legitimate, decision-0006-composed
   two-scenario state, where `Cigna`/`obesity` is *supposed* to already show
   elevated negative billing before `generate_denials.py` even runs.

**A third, self-inflicted near-miss, caught immediately**: while
independently verifying the CWD-path fix above, the coordinator ran
`generate_denials.py` directly against the real committed `healthcare.db`
(intending a quick sanity check, forgetting the cumulative-mutation gotcha
bug 2 above describes) — producing a corrupted intermediate state (1,788
`INVALID_BILLING_AMOUNT` denials, nowhere near the correct ~641). Caught via
`git diff --stat -- src/datahub/healthcare.db` immediately after, reverted
with `git checkout --`, confirmed clean, full test suite re-run to confirm
recovery. Left in as an honest example of exactly the class of mistake
item 2/3's fixes exist to prevent — including for people who already know
about the bug.

**`INVESTIGATOR_MAX_BUDGET_USD`'s default raised from $0.75 to $2.00**,
following this UAT session: two real, measured investigations
(`Cigna`/`obesity` at $0.88, `UnitedHealthcare`/`diabetes` at $0.75, this
run) both landed at or above the old default — a budget a correctly-working
investigation can plausibly exceed isn't a safety margin, it's a coin flip
on a confusing `BudgetExhaustedError` unrelated to whether the detection
logic works.
