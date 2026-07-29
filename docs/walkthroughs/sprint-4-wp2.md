# Sprint 4, WP2 — README polish + open-source contribution

Two independent halves, each with its own explicit stop for approval: a
judge-facing README polish pass (Part A), and turning two real DataHub
platform findings this project already made (decisions 0007 §3, 0011 §5)
into upstream GitHub issues (Part B/C). Neither part touched the other —
the README changes and the issue text were reviewed and approved on
separate tracks.

## Part A: README polish, not a rewrite

WP1's README was already functionally proven (the setup/command sequence
was fresh-clone tested twice), so this pass only added judge-facing
framing around it, untouched otherwise:

- **`## The pitch`** (originally drafted as `## What this is`, renamed on
  the repo owner's explicit correction to sit clearly distinct from the
  pre-existing `## What this is, and isn't` section further down) — the
  two-scenario contrast (`UnitedHealthcare`/`diabetes` →
  `introduced_at:claims` vs. `Cigna`/`obesity` →
  `inherited_from:raw_patients`, same detector proving real discrimination)
  and all four hackathon tracks, each mapped to the specific feature that
  earns it — not just named. First draft cited only "the Production ML
  judging track" (the one track name actually documented anywhere in this
  repo, in `register_ml_model.py`/`sprint-1-day1.md`) rather than
  guessing at others; the repo owner supplied the real four-track list
  from the original brief ("Agents That Do Real Work," "Metadata-Aware
  Code Generation & Development," "Production ML Agents," "Open/
  Wildcard") and asked for one clause per track showing the fit, not a
  bare list.
- **`## Quick links`** — `examples/`, a sample `audit_report.html`, both
  PR URLs, decisions 0008 and 0012 as the two most worth reading first,
  plus an honest current-state line (414 tests, 408 default/6 live, and
  the three measured run-time numbers with a link to `sprint-4-wp1.md`
  as the source).
- **All three `LLM_BACKEND` options named explicitly** in Prerequisites
  (`claude_code` default/no key needed, `anthropic` needs a key, `ollama`
  labeled honestly as an interface-only stub) — previously Ollama was
  only mentioned inside a `.env` comment, effectively invisible.

**A real verification incident, not just a correction.** Asked to paste
the actual "What this is" text for review (not a summary of the edit —
the repo owner's explicit standard, same reasoning as every live-test
requirement this project has held all week), the first paste came through
with words fused mid-token ("Generatiator," "check-drented"). The repo
owner did not accept "probably a rendering issue" as an answer and asked
for two specific, falsifiable checks instead: `sed -n '1,31p' README.md`
(read the actual file, character by character) and
`grep -c "Generatiator\|check-drented\|befen opens" README.md` (a
zero-or-nonzero test with no room for a hedged answer). Both run for
real: `sed` showed fully readable prose, `grep` returned `0`. The file
was never wrong — the corruption was introduced when quoting file content
into a chat response, not in the file itself. Lesson applied for the rest
of this work package: verify a fresh `Read`/`sed` output immediately
before pasting any long quoted block, rather than trusting an in-context
copy of something read earlier in the conversation.

## Part B: reviewing two real platform findings as issue candidates

This project had already independently discovered two real DataHub
platform limitations while building Sprint 1/3 (not sought out for this
work package — found in the ordinary course of registering a real
`MLModel` entity and building a feature-health check against it):

1. **No supported mechanism makes `MLModel` a lineage upstream of a
   `Dataset`** (decision 0007 §3, originally Sprint 1 —
   `register_ml_model.py`'s `build_output_reference_mcps`). Three
   independent approaches tried, all rejected server-side:
   `UpstreamLineageClass`/`UpstreamClass.dataset` (422, "Unable to
   instantiate urn type: DatasetUrn"), the `updateLineage` GraphQL
   mutation ("Tried to add lineage edge with non-dataset node when we
   expect a dataset"), and a `DataJob` detour (`DataJobInputOutputClass`
   only accepts Dataset URNs too).
2. **`CustomAssertionInfo`'s error messages don't explain the actual
   constraint** (decision 0011 §5, Sprint 3 WP4 — `drift.py`'s
   `_ensure_drift_assertion_defined`). `.entity` must be a Dataset URN
   (error: bare "Required: [dataset]", doesn't say what's required or
   why MLModel is rejected); `.field` must be a `schemaField` URN, not a
   bare feature name (error: "Failed to retrieve entity with urn
   segment_denial_rate, invalid urn" — reads like a does-not-exist error,
   not a URN-format one).

**Recommendation, approved unchanged**: lead with the lineage finding as
the stronger, primary issue — three independently exhausted approaches
beats one attempt, and it blocks a use case DataHub's own `MLModel`/
`MLFeature`/`MLFeatureTable` entities exist specifically to support.
File the assertion-error-messages finding too, but framed narrowly around
message clarity rather than asserting MLModel *should* be a valid
assertion entity — a design opinion a maintainer could reasonably reject,
versus "this error text didn't explain the real constraint," which is
much harder to dismiss. Same principle Remediator's own PR framing
already established (decision 0008): report what was observed, don't
prescribe what the platform's design should be.

Drafts were written to files and their content re-verified via `cat`
before being quoted back in chat — the same lesson from the README
paste, applied proactively this time rather than after a correction.
Two repo-owner refinements before approval: pin the "reproduction
available" offer to an actual commit+line-range GitHub permalink into
this repo rather than a vague "our repo" pointer (a maintainer who can
`git show` the exact attempt in 30 seconds is more likely to act than one
reconstructing it from prose), and keep a third, related finding
(`MLFeature.sources` also rejects column-level `schemaField` URNs) as a
one-paragraph footnote in the lineage issue rather than splitting it into
a third issue, to avoid reading as padded rather than thorough.

## Part C: filing, verified before and after

Before filing: the repo owner explicitly did not want the permalink SHA
trusted on re-derivation alone ("confirm by clicking the rendered link
once, not by re-deriving it"). The Chrome extension wasn't connected in
this environment, so the closest equivalent was used instead and stated
plainly as a substitution, not silently swapped in: `WebFetch` against
the actual rendered GitHub blob URLs for both permalinks, confirming each
resolved (not a 404), showed the correct file path and commit SHA, and
displayed the exact quoted error text at the claimed line range — for
both `register_ml_model.py#L240-L255` and `drift.py#L507-L534`.

Filed via `gh issue create` against `datahub-project/datahub`:

- Issue 1 (lineage): [datahub-project/datahub#18742](https://github.com/datahub-project/datahub/issues/18742)
- Issue 2 (assertion error messages): [datahub-project/datahub#18743](https://github.com/datahub-project/datahub/issues/18743)

Both verified after filing via `gh issue view --json` — body content
byte-matches the approved draft for both. One real, minor discrepancy
found and reported honestly rather than glossed over: the `--label bug`
flag was accepted by `gh issue create` with no error, but neither issue
actually carries the label (`gh issue view`'s `labels` field came back
empty for both). This is standard GitHub behavior for an external
contributor without write access to the target repo — label assignment
silently no-ops rather than erroring — not a defect in how the issues
were filed, and not worth re-filing over.

Both URLs recorded in the decision docs that originally documented each
finding: a new "Upstream issue filed (Sprint 4 WP2)" section appended to
`0007` and `0011` respectively, rather than one combined new decision
doc — keeps each finding's upstream-issue record next to the original
technical writeup it came from.
