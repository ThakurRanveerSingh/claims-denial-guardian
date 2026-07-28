# 0012 — FHIR Compliance Bridge design (Sprint 3 stretch, WP5)

Date: 2026-07-28
Status: Accepted

## Context

CMS-0057-F requires FHIR-based prior-auth/claims data exchange —
denial-metrics reporting has been mandatory since Jan 2026, full API
compliance is due Jan 2027. The pitch for this stretch work package:
Guardian's findings extend into CMS-mandated FHIR resources, flagging
which ones are built on data currently under active quality investigation.

Explicitly scoped as a **half-day, hard-capped thin slice**, done in-chat
(Part A design sketch, brief, before any code) rather than a standalone
LLD — matching the WP1–3 pattern, not WP4's. The repo owner's explicit
instruction going in: at the design STOP, scrutinize the "what's honestly
populatable vs. placeholder" line specifically, because a demo compliance
artifact that quietly overclaims regulatory readiness is a real risk this
project has avoided everywhere else. That instruction shaped every
decision below.

## Decision

### 1. EOB generation is deterministic templating — zero LLM

`fhir_export.py` builds each `ExplanationOfBenefit` resource with plain
Python dict construction from real `claims`/`denials` rows plus the
already-computed `InvestigatorFinding` — no model call anywhere in the
path. Same "translation, not judgment" law Sentinel/Scribe/Drift already
apply: there is no step here that benefits from judgment, only from
correctly executing already-decided facts (what the data says, what
Investigator already concluded).

### 2. Real vs. placeholder — the honesty line the repo owner asked to scrutinize

This project's `claims`/`denials` schema has no ICD-10, no CPT/HCPCS, no
CARC/RARC codes, no NPI, and no real Patient/Organization identifiers —
only free-text fields (`medical_condition`, `hospital`,
`insurance_provider`, `patient_name`) and an internal enum
(`denial_reason_code`). Every required R4 element was individually
checked against what's actually derivable:

- **`type` (required 1..1 CodeableConcept, claim-type classification)** —
  no real data exists anywhere in this schema for institutional vs.
  professional vs. any other claim-type distinction. Rather than pick a
  plausible-looking default (which would look authoritative and wouldn't
  be), this uses FHIR's own `data-absent-reason` extension mechanism
  (`http://hl7.org/fhir/StructureDefinition/data-absent-reason`,
  `valueCode: "unsupported"`): the element is structurally present
  (satisfies the required cardinality) but carries only the absent-reason
  extension, no coding, no text. This is the correct FHIR-native pattern
  for a complex-typed required element with no real value — not a
  workaround, not an omission, the standard's own built-in way to tell
  the truth about a genuine gap.
- **`diagnosis[].diagnosisCodeableConcept`** — `.text` only
  (`medical_condition`'s raw string, e.g. "obesity"). `.coding` is
  deliberately never populated — no real ICD-10 mapping exists for this
  field, and fabricating one would look authoritative without being
  real.
- **`item[].adjudication[].reason`** — same discipline, `.text` only
  (`denial_reason_code`, e.g. "INVALID_BILLING_AMOUNT" — this project's
  own internal enum, not a real CARC/RARC code).
- **`patient` / `insurer` / `provider`** — `Reference.display` only
  (`patient_name` / `insurance_provider` / `hospital`, carried through
  as-is, messy source casing and all). No `.reference` is ever emitted —
  no Patient/Organization/Coverage resources exist in this slice
  (explicitly out of scope, §5), so a resolvable reference would claim a
  real resource that was never created.
- **`status`, `use`, `outcome`** — fixed, but honestly true structural
  values, not placeholders: `status: "active"` (a live-status flag, not
  a claim about the data), `use: "claim"` (these are real retrospective
  denied claims, not prior-auth requests), `outcome: "complete"` (real
  FHIR semantics — adjudication finished; the denial itself lives in
  `disposition` and the adjudication amount, which is what `outcome`'s
  actual valueset is for).
- **`net`/`total`/adjudication `amount`** — `billing_amount`/
  `denial_amount`, carried through unaltered, including negative values.
  For both canonical incidents, the sampled claims' `billing_amount` IS
  negative — the exact data-quality defect Guardian's Investigator is
  reporting on. This is deliberate: the exported compliance artifact
  visibly carries the defect it's flagging, in situ, rather than a
  sanitized number that would hide the story the extension is telling.

### 3. Sampling is bounded and reason-code-scoped, not a bulk export

incident.json carries segment-level aggregates, not per-claim rows, and
both canonical incidents flag hundreds of denied claims. Exporting all of
them isn't a thin slice. `run_fhir_export()` samples a small, bounded set
(default 3, `--limit` overridable) of the segment's claims **filtered to
`denial_reason_code = "INVALID_BILLING_AMOUNT"`** specifically — the one
reason code both canonical incidents' root-cause breakdowns already
implicate (confirmed against each incident.json's own
`root_cause_breakdown` before hardcoding this, not assumed) — rather than
an arbitrary cross-section of the segment that might pick
`HIGH_RISK_SCORE`/`RANDOM_AUDIT` denials with no defect to show at all.
Same one-concrete-expectation scoping discipline decision 0007 already
established for Scribe's billing-amount assertion, applied here to
sampling instead of a generic reason-code mapper. `ORDER BY claim_id`
makes the sample deterministic run-to-run — same limit, same claims, same
bytes — which is what makes the idempotency proof in Part C actually
provable rather than merely claimed.

### 4. The data-quality flag: a namespaced custom extension, not a fake official one

Each resource carries a `guardian-data-quality-flag` extension (plus a
lighter `meta.tag` for at-a-glance scanning) with `incidentId`,
`classification` (verbatim from `InvestigatorFinding.primary_root_cause`
— this IS the `introduced_at:claims` vs. `inherited_from:raw_patients`
distinction the design asked for, not a re-derived summary), `confidence`,
and an `evidence` link to the incident's own committed `incident.json` on
GitHub. The extension's canonical URL is built live from the repo's own
`git remote get-url origin` (same pattern scribe.py's `_github_blob_url`
already established, duplicated per this codebase's "small boilerplate,
copied not shared" convention) and points at this decision doc — a real,
resolvable page describing what the extension means, not a fabricated
`/fhir/StructureDefinition/...` path implying a hosted, registered
profile that doesn't exist. Degrades to a non-resolving `urn:guardian:fhir`
if no GitHub remote is configured, same graceful-degradation shape as
`doc_url` elsewhere in this codebase. Deliberately NOT namespaced under
any real HL7/Da Vinci URL — an extension that looked like an official IG
extension but wasn't would be actively misleading, worse than one that's
honestly and visibly ours.

### 5. DataHub registration reuses Scribe's writeback pattern — no new mechanism

One persistent dataset entity represents the export ARTIFACT TYPE
(`urn:li:dataset:(urn:li:dataPlatform:file,healthcare.guardian_exports.
fhir_explanation_of_benefit,PROD)`), not one entity per incident or per
claim — same "reuse, don't multiply" discipline as Scribe's single
`guardian-incident` tag. Platform `file` is the honest label: this is
JSON written to disk, not a queryable table (mirrors
`register_ml_model.py`'s `python` platform for a model that isn't tied to
any ML framework — the label says what the thing actually is). Tag and
per-incident doc note (`[incident_id] ...` prefix, same dedup convention
`_parse_doc_entries` already uses) are exactly Scribe's existing
tag/institutionalMemory pattern, retargeted — the same reuse `drift.py`
already demonstrated onto a different entity type. Lineage
(`FHIR_EXPORT_DATASET_URN <- raw_patients`) is resolved live via MCP
search, never hardcoded, same `_resolve_entity_urn` pattern.

Live-verified, not assumed: exporting the second canonical incident
correctly reported `tag already present` (same shared artifact-type
entity as the first incident) while still adding a fresh, distinct doc
note for its own `incident_id` — proving the entity-level idempotency and
the incident-level append-only behavior are both working as designed, not
just one or the other.

### 6. Out of scope, explicitly

No FHIR server (flat files only, no persistence/query API). No
OAuth/SMART-on-FHIR. No profile validation against Da Vinci PAS/PDex
Payer-Network IGs — Part B's tests are structural only (valid JSON,
required-element presence), not real HL7 conformance validation. No
Patient/Coverage/Claim resources, no Bundle. No real ICD-10/CPT/CARC code
systems anywhere. No `guardian run` pipeline integration — `export-fhir`
is a standalone command a user runs against an already-investigated
incident.

## Alternatives considered

- **A plausible default claim-type code (e.g. always
  "professional").** Rejected — would look authoritative without being
  real; exactly the "sounds more complete, not honest" trap this project
  has rejected everywhere else (decision 0011 §2 rejected the same trap
  for a different check). FHIR's own data-absent-reason mechanism exists
  precisely for this case.
- **Exporting every denied claim in the segment.** Rejected — hundreds of
  resources per incident isn't a thin slice; a bounded, reason-code-scoped
  sample proves the mechanism just as well.
- **One DataHub dataset entity per incident (or per claim).** Rejected —
  multiplies entities for no benefit; one persistent artifact-type entity
  with per-incident doc notes matches Scribe's existing tag-reuse
  discipline.
- **A real-looking Da Vinci-style extension URL.** Rejected — would
  misrepresent an ad hoc, unregistered extension as part of an official
  implementation guide.
- **Embedding editorial commentary ("this is the defect under
  investigation") inside the FHIR resource itself.** Rejected — would
  conflate a resource meant to look like genuine payer output with
  Guardian's own narrative; the extension mechanism already carries the
  structured linkage, and the human framing belongs in this doc and the
  walkthrough, not inside the JSON (same restraint decision 0009 already
  established for keeping raw agent telemetry out of the compliance-facing
  narrative, applied here in reverse).

## Consequences

- `src/agents/fhir_export.py` is a new, standalone module — zero LLM
  calls, same "code verifies, LLM never decides" boundary every
  deterministic stage in this codebase keeps. Not wired into
  `guardian run`'s pipeline or `Incident`'s schema — a standalone
  `guardian export-fhir <incident_id>` command, deliberately decoupled so
  this stretch slice couldn't expand the core pipeline's contract under a
  half-day cap.
- Both canonical incidents now have real, live-generated
  `examples/<incident_id>/fhir/eob-*.json` resources and a real DataHub
  writeback — not backfilled by hand.
- The two canonical incidents happen to cover both `classification`
  framings the design called out (`inherited_from:raw_patients` for
  Cigna/obesity, `introduced_at:claims` for UnitedHealthcare/diabetes) —
  confirmed live in the generated resources, not just asserted in this
  doc.
- Time cap: Part A's design sketch ran ~20 minutes against a 15-minute
  target (spent reading real schema/data rather than guessing).
  Implementation and structural tests (Part B) were fast, reusing
  Scribe/Drift's writeback code directly. The live proof (Part C) was the
  slow part in wall-clock terms — each `guardian export-fhir` invocation
  took several minutes, almost entirely spent in the DataHub MCP
  subprocess's own telemetry-retry backoff (`track.datahubproject.io`
  connection timeouts, ~40s of retries per MCP call) rather than in this
  module's own code. Reported honestly here rather than rounded down:
  the cap held on engineering time, but the wall-clock total for this
  session ran longer than the design's own time-check anticipated, purely
  from external service latency outside this project's control.
