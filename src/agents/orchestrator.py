#!/usr/bin/env python3
"""
Orchestrator — the Sentinel -> Investigator pipeline, and the `Incident`
record that captures one run of it. Design: docs/architecture/lld-sprint2.md
§4.

Explicitly a SUBSET of hld.md §2.4's eventual four-stage pipeline
(Sentinel -> Investigator -> Remediator -> Scribe), per §4.1 — Remediator
(US-3) and Scribe (US-4/US-5) are later sprints, not built here.
`Incident`'s shape (§4.2) is deliberately built so those two stages can
append their own sections to an existing record later, rather than this
contract needing to change shape when they're added.

What this module does, in one sentence: run Sentinel (Slice 1) across every
segment, decide which ones need Investigator (Slice 3) — either because
Sentinel flagged them, or because a caller forced one via `segment=` — run
it on those, and assemble one `Incident` per segment scanned (§4.2's own
status enum includes "no_anomaly", which only makes sense if an Incident
object exists for the segments that didn't need investigating too — it's
just never WRITTEN to disk for those, per §4.2's explicit "only when
status != no_anomaly" rule).

Sequential, not parallel, for flagged segments needing investigation — same
failure-isolation reasoning decision 0004 already establishes for
Investigator's own loop (a stuck or failed investigation on one segment
shouldn't complicate diagnosing another).
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from agents.drift import DriftFinding, FeatureHealthCheck
from agents.investigator import EvidenceEntry, InvestigatorFinding, RootCauseBreakdownEntry, run_investigator
from agents.llm_backend import LLMBackend, LLMBackendError, get_backend
from agents.remediator import FixTarget, FreshBuildResult, RemediationAttempt, RemediatorResult, ValidationResult, run_remediator
from agents.reporter import write_audit_reports
from agents.scribe import ScribeEntityResult, ScribeResult, run_scribe
from agents.sentinel import Segment, SentinelFinding, run_sentinel

# Same load_dotenv() convention as every other module in this repo.
load_dotenv()

_MODULE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _MODULE_DIR.parent.parent

# Same absolute-path convention src/agents/investigator.py already
# established (removes any dependency on the calling process's cwd).
DB_PATH = (_REPO_ROOT / "src" / "datahub" / "healthcare.db").resolve()

# §4.2: "examples/<incident-id>/incident.json" — the exact location
# hld.md §2.5 already designated for per-incident output.
EXAMPLES_DIR = (_REPO_ROOT / "examples").resolve()


# ---------------------------------------------------------------------------
# Incident — §4.2's structured findings object, implemented for real.
# ---------------------------------------------------------------------------


@dataclass
class IncidentCost:
    """§4.2's `cost` sub-object."""

    sentinel_llm_calls: int
    investigator_turns_or_calls: Optional[int]
    investigator_cost_usd: Optional[float]
    wall_clock_seconds: float


@dataclass
class Incident:
    """§4.2's contract, all fields. `investigator` is `None` exactly when
    `status == "no_anomaly"` — Sentinel's clean-run case is a valid,
    expected outcome, not a degenerate "investigation," so there's nothing
    to force into that shape (§4.2's own reasoning, restated here since this
    is the first slice that actually builds one)."""

    incident_id: str
    created_at: str  # ISO 8601
    status: str  # "no_anomaly" | "investigated" | "inconclusive"
    pipeline_stages_run: list[str]  # ["sentinel"], [..., "investigator"], or [..., "scribe"]
    sentinel: SentinelFinding
    investigator: Optional[InvestigatorFinding]
    cost: IncidentCost
    # Sprint 3 WP1: the extension lld-sprint2.md §4.1 explicitly anticipated
    # ("Remediator/Scribe... append their own sections to an existing
    # record later, rather than requiring this sprint's contract to be
    # reopened"). None whenever Scribe didn't run (no_anomaly, --dry-run,
    # or --no-writeback) — same "None means genuinely didn't happen, not a
    # placeholder" convention `investigator` already uses.
    scribe: Optional[ScribeResult] = None
    # Sprint 3 WP2: same "None means genuinely didn't happen" convention as
    # `scribe` above. None unless --remediate was passed AND an
    # investigation actually ran for this segment — opening a PR is a real
    # side effect, off by default (decision 0008), same principle as
    # --no-writeback for Scribe's DataHub writes.
    remediator: Optional[RemediatorResult] = None
    # Sprint 3 WP4: NOT incident-specific like scribe/remediator above —
    # a DriftFinding is a model-level feature-health check, computed by
    # `guardian check-drift` independently of any one investigation, then
    # optionally attached to a saved incident (`--incident <id>`,
    # `orchestrator.attach_drift_finding()`) so its audit report can carry
    # a Model Health Check section. None whenever no check has been
    # attached to this incident — same "None means genuinely didn't
    # happen" convention as scribe/remediator.
    drift: Optional[DriftFinding] = None

    def to_dict(self) -> dict:
        """JSON-serializable form for examples/<incident-id>/incident.json.

        NOT a bare `dataclasses.asdict(self)`: `SentinelFinding.segment` is a
        `Segment` NamedTuple (src/agents/sentinel.py), and `asdict()`
        recurses into NamedTuples by RECONSTRUCTING the same NamedTuple type
        rather than converting it to a dict — which means a naive
        `json.dumps(asdict(incident))` would silently serialize `segment` as
        a bare 2-element JSON ARRAY (`["UnitedHealthcare", "diabetes"]`),
        losing the `insurance_provider`/`medical_condition` field names
        entirely. Verified this directly during implementation rather than
        assuming `asdict()` "just works" for JSON output — a real, if easy
        to miss, gap. `Segment._asdict()` (NamedTuple's own built-in method)
        is what actually preserves the field names here.
        """
        return {
            "incident_id": self.incident_id,
            "created_at": self.created_at,
            "status": self.status,
            "pipeline_stages_run": self.pipeline_stages_run,
            "sentinel": _sentinel_finding_to_dict(self.sentinel),
            "investigator": self.investigator.to_dict() if self.investigator is not None else None,
            "cost": asdict(self.cost),
            # ScribeResult/ScribeEntityResult are plain dataclasses with no
            # NamedTuple fields -- none of the Segment-style asdict() gotcha
            # this method's own docstring warns about applies here.
            "scribe": asdict(self.scribe) if self.scribe is not None else None,
            # Same reasoning: RemediatorResult/RemediationAttempt/FixTarget/
            # ValidationResult are all plain dataclasses, no NamedTuple
            # fields anywhere in the chain.
            "remediator": asdict(self.remediator) if self.remediator is not None else None,
            # DriftFinding/FeatureHealthCheck are also plain dataclasses —
            # same no-NamedTuple-gotcha reasoning as scribe/remediator.
            "drift": asdict(self.drift) if self.drift is not None else None,
        }


def _sentinel_finding_to_dict(f: SentinelFinding) -> dict:
    d = asdict(f)
    d["segment"] = dict(f.segment._asdict())  # see Incident.to_dict()'s docstring for why this matters
    return d


def _slugify(value: str) -> str:
    """Lowercase, non-alphanumeric runs collapsed to single hyphens, no
    leading/trailing hyphens — "Blue Cross" -> "blue-cross"."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def _make_incident_id(segment: Segment, when: datetime) -> str:
    """§4.2: "INC-<YYYYMMDDTHHMMSSZ>-<provider-slug>-<condition-slug>",
    timestamped to the SECOND (not just the date) — re-running the same
    segment twice in one day shouldn't collide. Same "derived, traceable,
    not a bare UUID" philosophy as claim_id (lld-sprint1.md §1.1)."""
    ts = when.strftime("%Y%m%dT%H%M%SZ")
    return f"INC-{ts}-{_slugify(segment.insurance_provider)}-{_slugify(segment.medical_condition)}"


def write_incident(incident: Incident, examples_dir: Path = EXAMPLES_DIR) -> Path:
    """§4.2: written as examples/<incident-id>/incident.json — CALLER's
    responsibility to only call this when status != "no_anomaly" (build_
    incident_for_segment / run_guardian below already only call this for
    incidents that need it; kept as a separate function, not folded into
    incident construction, so a caller can inspect/decide before writing).
    """
    incident_dir = examples_dir / incident.incident_id
    incident_dir.mkdir(parents=True, exist_ok=True)
    path = incident_dir / "incident.json"
    path.write_text(json.dumps(incident.to_dict(), indent=2, default=str))
    return path


def _load_scribe_result(data: Optional[dict]) -> Optional[ScribeResult]:
    if data is None:
        return None
    return ScribeResult(
        incident_id=data["incident_id"],
        entities=[ScribeEntityResult(**e) for e in data.get("entities", [])],
        doc_url=data.get("doc_url"),
    )


def _load_remediator_result(data: Optional[dict]) -> Optional[RemediatorResult]:
    if data is None:
        return None
    ft = data.get("fix_target")
    fix_target = None
    if ft is not None:
        # upstream_tables round-trips through JSON as a list -- FixTarget's
        # real field is a tuple (it's a frozen/hashable dataclass); restore
        # that rather than leave it silently as a list.
        fix_target = FixTarget(
            transform_file=ft["transform_file"], table_name=ft["table_name"],
            upstream_tables=tuple(ft["upstream_tables"]), check_column=ft["check_column"],
        )
    attempts = []
    for a in data.get("attempts", []):
        validation = ValidationResult(**a["validation"])
        fresh_build = FreshBuildResult(**a["fresh_build"]) if a.get("fresh_build") is not None else None
        attempts.append(RemediationAttempt(attempt_number=a["attempt_number"], sql=a["sql"], validation=validation, fresh_build=fresh_build))
    return RemediatorResult(
        incident_id=data["incident_id"], status=data["status"], fix_target=fix_target, attempts=attempts,
        pr_url=data.get("pr_url"), pr_already_existed=data.get("pr_already_existed", False),
        pr_updated=data.get("pr_updated", False), branch_name=data.get("branch_name"),
        owner=data.get("owner"), reason=data.get("reason"),
    )


def _load_drift_finding(data: Optional[dict]) -> Optional[DriftFinding]:
    if data is None:
        return None
    return DriftFinding(
        check_id=data["check_id"], model_version=data["model_version"], checked_at=data["checked_at"],
        feature_checks=[FeatureHealthCheck(**c) for c in data.get("feature_checks", [])],
        overall_status=data.get("overall_status", "pass"),
    )


def load_incident(path: Path) -> Incident:
    """The inverse of `write_incident()` / `Incident.to_dict()` — reconstructs
    a full `Incident` from a saved `incident.json`, including `.scribe`/
    `.remediator` as real dataclass objects (not raw dicts) when the JSON
    has them. Promoted here from what was, until Sprint 3 WP3, duplicated ad
    hoc inside `tests/test_scribe.py`'s and `tests/test_remediator.py`'s own
    live tests and one-off scratch scripts — this is now the one real place
    that reconstruction logic lives. `resume_incident()` below and Sprint 3
    WP3's Reporter (`src/agents/reporter.py`) both use it directly instead
    of re-deriving it again; Reporter specifically needs `.scribe`/
    `.remediator` as real objects (attribute access, e.g.
    `result.fix_target.transform_file`), which is why those two are fully
    reconstructed here rather than left as raw dicts the way an earlier
    version of this function did.
    """
    data = json.loads(path.read_text())

    s = data["sentinel"]
    sentinel = SentinelFinding(
        segment=Segment(s["segment"]["insurance_provider"], s["segment"]["medical_condition"]),
        segment_claim_count=s["segment_claim_count"], segment_denial_count=s["segment_denial_count"],
        segment_denial_rate=s["segment_denial_rate"], baseline_denial_rate=s["baseline_denial_rate"],
        z_score=s["z_score"], threshold=s["threshold"], method=s["method"], flagged=s["flagged"], summary=s["summary"],
    )

    investigator = None
    if data.get("investigator") is not None:
        i = data["investigator"]
        investigator = InvestigatorFinding(
            primary_root_cause=i["primary_root_cause"],
            root_cause_breakdown=[RootCauseBreakdownEntry(**e) for e in i["root_cause_breakdown"]],
            affected_branch=i["affected_branch"], datasets_checked_and_clean=i["datasets_checked_and_clean"],
            lineage_path_walked=i["lineage_path_walked"],
            evidence=[EvidenceEntry(**e) for e in i["evidence"]],
            root_cause_summary=i["root_cause_summary"], confidence=i["confidence"],
            backend_used=i["backend_used"], turns_used=i["turns_used"],
        )

    return Incident(
        incident_id=data["incident_id"], created_at=data["created_at"], status=data["status"],
        pipeline_stages_run=data["pipeline_stages_run"], sentinel=sentinel, investigator=investigator,
        cost=IncidentCost(**data["cost"]),
        scribe=_load_scribe_result(data.get("scribe")),
        remediator=_load_remediator_result(data.get("remediator")),
        drift=_load_drift_finding(data.get("drift")),
    )


def attach_drift_finding(
    incident_id: str, finding: DriftFinding, *,
    examples_dir: Path = EXAMPLES_DIR,
    healthcare_db_path: Optional[Path] = None,
) -> Incident:
    """Attaches an already-computed `DriftFinding` (from
    `drift.run_drift_check()`) to a previously saved incident, re-saves
    `incident.json`, and regenerates that incident's audit report so its
    Model Health Check section reflects the attached finding. This is what
    `guardian check-drift --incident <id>` (LLD §3.5) uses — composes with
    `load_incident()`/`write_incident()` the same way `resume_incident()`
    does above, rather than inventing a second reload mechanism for what's
    the same underlying operation (attach a downstream result to an
    existing incident record).

    Deliberately NOT folded into `resume_incident()`'s `stage=` dispatch:
    that function's two existing stages both consume
    `incident.investigator` to do their work (Remediator/Scribe act on a
    specific investigation's findings); a drift check is model-level, not
    incident-specific — it doesn't read anything from `incident` at all,
    it's attached to one purely so the report can show it alongside a
    real investigation.
    """
    incident = load_incident(examples_dir / incident_id / "incident.json")
    incident.drift = finding
    if "drift" not in incident.pipeline_stages_run:
        incident.pipeline_stages_run = incident.pipeline_stages_run + ["drift"]
    write_incident(incident, examples_dir=examples_dir)
    write_audit_reports(incident, examples_dir=examples_dir, healthcare_db_path=healthcare_db_path or Path(DB_PATH))
    return incident


def resume_incident(
    incident_id: str, stage: str, *,
    examples_dir: Path = EXAMPLES_DIR,
    backend: Optional[LLMBackend] = None, llm_backend_name: Optional[str] = None,
    healthcare_db_path: Optional[Path] = None, data_platform_repo_path: Optional[Path] = None,
) -> Incident:
    """Resume a previously saved incident from a given pipeline stage
    onward — WITHOUT re-running Sentinel/Investigator. This is a real,
    supported operation in its own right, not a one-off patch: Sentinel and
    Investigator are the expensive, non-deterministic stages (a fresh
    Investigator run mints a NEW `incident_id`, timestamped to when it ran,
    and — since Remediator's PR branch name is derived directly from
    `incident_id` — re-running the full pipeline can never "backfill" an
    existing incident's later-stage fields; it can only ever produce a
    sibling incident with its own new PR). `resume_incident()` instead
    loads the EXACT existing `Incident` (same `incident_id`, same
    `SentinelFinding`/`InvestigatorFinding` already on record) and runs only
    the requested downstream stage against it — so `_branch_name_for`
    resolves to the SAME branch a prior Remediator run already pushed to,
    and `_existing_pr_url` correctly recognizes an already-open PR instead
    of treating this as a fresh incident needing a fresh one.

    `stage`: `"remediate"` (built for Sprint 3 WP3: backfilling
    `incident.json`'s `remediator` field for the two canonical demo
    incidents from a real `run_remediator()` call, not a throwaway script)
    or `"writeback"` (built the same session, same need: backfilling
    `incident.json`'s `scribe` field from a real `run_scribe()` call —
    Scribe is idempotent per decision 0007, so resuming it against an
    incident whose DataHub tag/doc/assertion were already applied by an
    earlier, disconnected run correctly reports `*_already_present=True`
    rather than duplicating anything; verified live, not assumed, before
    this was trusted for the real backfill).
    """
    incident = load_incident(examples_dir / incident_id / "incident.json")

    if stage == "remediate":
        if incident.investigator is None:
            raise ValueError(f"{incident_id} has no InvestigatorFinding — nothing to remediate")
        if backend is None:
            backend = get_backend(llm_backend_name)
        kwargs = {}
        if healthcare_db_path is not None:
            kwargs["healthcare_db_path"] = healthcare_db_path
        if data_platform_repo_path is not None:
            kwargs["data_platform_repo_path"] = data_platform_repo_path
        incident.remediator = run_remediator(incident, backend, **kwargs)
        if "remediator" not in incident.pipeline_stages_run:
            incident.pipeline_stages_run = incident.pipeline_stages_run + ["remediator"]
    elif stage == "writeback":
        if incident.investigator is None:
            raise ValueError(f"{incident_id} has no InvestigatorFinding — nothing to write back")
        incident.scribe = run_scribe(incident)
        if "scribe" not in incident.pipeline_stages_run:
            incident.pipeline_stages_run = incident.pipeline_stages_run + ["scribe"]
    else:
        raise ValueError(f"resume_incident: unsupported stage {stage!r} (only 'remediate'/'writeback' are implemented)")

    return incident


# ---------------------------------------------------------------------------
# Pipeline.
# ---------------------------------------------------------------------------


def _build_incident(
    sentinel_finding: SentinelFinding,
    *,
    investigator_finding: Optional[InvestigatorFinding],
    investigator_cost_usd: Optional[float],
    investigator_turns_or_calls: Optional[int],
    wall_clock_seconds: float,
    created_at: datetime,
    scribe_result: Optional[ScribeResult] = None,
) -> Incident:
    if investigator_finding is None:
        status = "no_anomaly"
        pipeline_stages_run = ["sentinel"]
    elif investigator_finding.primary_root_cause == "inconclusive":
        status = "inconclusive"
        pipeline_stages_run = ["sentinel", "investigator"]
    else:
        status = "investigated"
        pipeline_stages_run = ["sentinel", "investigator"]

    if scribe_result is not None:
        pipeline_stages_run = pipeline_stages_run + ["scribe"]

    return Incident(
        incident_id=_make_incident_id(sentinel_finding.segment, created_at),
        created_at=created_at.isoformat(),
        status=status,
        pipeline_stages_run=pipeline_stages_run,
        sentinel=sentinel_finding,
        investigator=investigator_finding,
        scribe=scribe_result,
        cost=IncidentCost(
            # Sentinel's optional LLM narration (§1.4's narrate_fn seam,
            # src/agents/sentinel.py) is still completely unwired as of this
            # slice — nothing in this codebase constructs or passes a
            # narrate_fn anywhere yet, so this is an accurate 0, not a
            # placeholder standing in for a number nobody computed.
            sentinel_llm_calls=0,
            investigator_turns_or_calls=investigator_turns_or_calls,
            investigator_cost_usd=investigator_cost_usd,
            wall_clock_seconds=wall_clock_seconds,
        ),
    )


def run_guardian(
    *,
    conn: Optional[sqlite3.Connection] = None,
    backend: Optional[LLMBackend] = None,
    llm_backend_name: Optional[str] = None,
    segment: Optional[tuple[str, str]] = None,
    dry_run: bool = False,
    writeback: bool = True,
    remediate: bool = False,
    generate_reports: bool = True,
    z_threshold: Optional[float] = None,
    max_budget_usd: Optional[float] = None,
    healthcare_db_path: Optional[Path] = None,
    on_investigating: Optional[Callable[[Segment], None]] = None,
) -> list[Incident]:
    """Run the full pipeline (§4.1): Sentinel across every segment, then
    Investigator on whichever ones need it. Returns one Incident PER SEGMENT
    Sentinel scanned (matches §4.2's status enum including "no_anomaly" —
    that value only makes sense if an Incident object exists for segments
    that didn't need investigating too). Callers that only care about the
    "interesting" ones filter on `.status != "no_anomaly"` themselves — this
    function doesn't drop information a caller might want (e.g. the CLI's
    --dry-run mode, which needs to see every segment's flagged status even
    though none of them get investigated).

    `segment`: forces Investigator to run against exactly this
    `(provider, condition)` pair, REGARDLESS of whether Sentinel flagged it
    (§4.3's "--segment" override) — and, when given, SCOPES investigation to
    ONLY that one segment (other segments still get their normal
    Sentinel-only Incident, just never investigated even if they happen to
    also be flagged). Sentinel itself still scans all 30 segments regardless
    of this override — the leave-one-out z-test (§1.2) needs the full
    population to compute correct baselines; only WHICH segment(s) get
    Investigator is scoped.

    `dry_run`: Sentinel only. No `LLMBackend` is ever constructed in this
    mode (not even to validate one exists) — `--dry-run`'s whole point is
    "zero LLM budget spent" (§4.3), and constructing a backend that then
    goes unused could itself fail loudly for an unrelated reason (e.g. a
    missing ANTHROPIC_API_KEY) that a dry run shouldn't care about at all.

    `backend`: supply a pre-built backend directly (tests, or a caller that
    already has one); otherwise built lazily via `get_backend(llm_backend_name)`
    the FIRST time an investigation is actually about to run — not
    up front — so dry runs and clean runs (nothing flagged, nothing forced)
    never pay that construction cost or its potential failure mode at all.

    `writeback`: whether Scribe (Sprint 3 WP1, docs/decisions/0007) runs
    after Investigator for each segment that was actually investigated
    (§4.3's `--no-writeback` override). Scribe never runs at all for
    `--dry-run` or a not-flagged/not-forced segment — there is no
    `InvestigatorFinding` to write back for those, matching the same
    "don't do the work if there's nothing to do" reasoning `backend`'s lazy
    construction already follows. Scribe's own internal logic additionally
    no-ops gracefully for an inconclusive finding (empty `affected_branch`,
    nothing to write) — not special-cased here, since Scribe already
    handles it.

    `remediate`: whether Remediator (Sprint 3 WP2, docs/decisions/0008) runs
    after Scribe for each segment that was actually investigated (`--remediate`
    override). OFF BY DEFAULT, deliberately — opening a real pull request is
    a side effect with its own consent story, same principle as
    `--no-writeback`'s default-on/opt-out shape, just inverted (default-off/
    opt-in) because a PR is a more visible, harder-to-quietly-ignore side
    effect than a DataHub tag. Independent of `writeback`: `--remediate`
    with `--no-writeback` still opens a PR without touching DataHub: the two
    are separate side effects on separate systems, not sequenced
    dependencies of each other. Remediator's own internal logic no-ops
    gracefully (`status="no_fix_available"`) for an inconclusive finding or
    a root cause with no known fix shape — not special-cased here, same
    "the agent already handles it" reasoning as `writeback` above.

    `generate_reports`: whether Reporter (Sprint 3 WP3, US-5) runs after
    Remediator for each segment that was actually investigated, writing
    `examples/<incident-id>/report/{audit_report.md,audit_report.html}`.
    ON BY DEFAULT, unlike `writeback`/`remediate` — deliberately, and for a
    real reason: Reporter is a pure read (the real `healthcare.db`, plus
    whatever `Incident` fields are already in memory) followed by a local
    file write. No DataHub write, no PR, no external system touched at
    all — there is no consent story to gate the way there is for the other
    two, so this parameter exists as a programmatic escape hatch (tests
    exercising a segment that doesn't exist in the real `healthcare.db`,
    which Reporter's own live db queries would otherwise fail against), not
    a user-facing CLI flag.

    `healthcare_db_path`: the real db file Reporter opens its OWN read-only
    connection against (member-impact counts, the z-test's raw baseline
    counts) — independent of `conn` above, which may be an in-memory or
    test-fixture connection Reporter has no file path for. Defaults to this
    module's own `DB_PATH` (the real, committed database) when not given.

    `on_investigating`: optional callback invoked with the `Segment` right
    before Investigator actually starts for it — this module stays print-
    free otherwise (that's cli.py's job), but Investigator's own call can
    silently run several minutes in practice (mostly DataHub MCP telemetry-
    retry backoff, not this codebase's own work — Sprint 4 WP1's fresh-clone
    judge simulation found a `guardian run` with zero terminal output for
    10+ minutes reads as a hang, not "working"). A caller that wants that
    reassurance (the CLI does) supplies a callback; a caller that doesn't
    (every test) just omits it and gets the previous, silent behavior.
    """
    own_conn = conn is None
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))

    try:
        sentinel_findings = run_sentinel(conn, z_threshold=z_threshold)

        forced_segment: Optional[Segment] = None
        if segment is not None:
            forced_segment = Segment(*segment)
            known_segments = {f.segment for f in sentinel_findings}
            if forced_segment not in known_segments:
                raise ValueError(
                    f"--segment {segment[0]!r},{segment[1]!r} was not found among the "
                    f"{len(sentinel_findings)} segments Sentinel scanned — check spelling/casing "
                    f"against the real data (e.g. medical_condition is lowercase in claims)."
                )

        incidents: list[Incident] = []
        for sf in sentinel_findings:
            created_at = datetime.now(timezone.utc)
            started = time.monotonic()

            should_investigate = not dry_run and (
                (forced_segment is not None and sf.segment == forced_segment)
                or (forced_segment is None and sf.flagged)
            )

            if not should_investigate:
                incidents.append(
                    _build_incident(
                        sf,
                        investigator_finding=None,
                        investigator_cost_usd=None,
                        investigator_turns_or_calls=None,
                        wall_clock_seconds=time.monotonic() - started,
                        created_at=created_at,
                    )
                )
                continue

            if backend is None:
                backend = get_backend(llm_backend_name)

            if on_investigating is not None:
                on_investigating(sf.segment)

            run_result = run_investigator(backend, sf, conn, max_budget_usd=max_budget_usd)
            incident = _build_incident(
                sf,
                investigator_finding=run_result.finding,
                investigator_cost_usd=run_result.cost_usd,
                investigator_turns_or_calls=run_result.finding.turns_used,
                wall_clock_seconds=time.monotonic() - started,
                created_at=created_at,
            )
            if writeback:
                # Mutate in place rather than rebuild: _build_incident()
                # already computed everything else correctly above (status,
                # incident_id, cost) -- only pipeline_stages_run and scribe
                # need updating once Scribe's real result is known, and
                # Incident is a plain (non-frozen) dataclass, so this is the
                # direct way to do that without recomputing the rest.
                incident.scribe = run_scribe(incident)
                incident.pipeline_stages_run = incident.pipeline_stages_run + ["scribe"]
            if remediate:
                # Same in-place mutation as `writeback` above. Independent
                # of it (see run_guardian()'s docstring) — runs regardless
                # of whether Scribe just ran or was skipped.
                incident.remediator = run_remediator(incident, backend)
                incident.pipeline_stages_run = incident.pipeline_stages_run + ["remediator"]
            if generate_reports:
                # Unconditional by design (default True, no CLI flag,
                # unlike writeback/remediate) — a pure read + local file
                # write has no external side effect and therefore no
                # consent story to gate. `generate_reports=False` exists as
                # a programmatic escape hatch (tests using a segment that
                # doesn't exist in the real healthcare.db, which Reporter's
                # own live db queries would otherwise fail against), not a
                # user-facing option.
                write_audit_reports(incident, examples_dir=EXAMPLES_DIR, healthcare_db_path=healthcare_db_path or Path(DB_PATH))
                incident.pipeline_stages_run = incident.pipeline_stages_run + ["report"]
            incidents.append(incident)

        return incidents
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Run summary output — §4.4, plus the repo owner's explicit Slice 4 ask
# (the lineage path Investigator actually walked, its own line).
# ---------------------------------------------------------------------------


def print_incident_summary(incident: Incident, written_path: Optional[Path] = None) -> None:
    """§4.4's illustrative shape, implemented for real."""
    print(f"Guardian run complete — {incident.incident_id}")
    print()
    print("Sentinel:")
    s = incident.sentinel
    print(f"  Segment: {s.segment.insurance_provider} / {s.segment.medical_condition}")
    print(
        f"  Denial rate: {s.segment_denial_rate:.1%} ({s.segment_denial_count}/{s.segment_claim_count}) "
        f"vs. {s.baseline_denial_rate:.1%} baseline (z = {s.z_score:.2f}, threshold {s.threshold})"
    )
    print(f"  → {'FLAGGED' if s.flagged else 'not flagged'}")
    print()

    if incident.investigator is not None:
        inv = incident.investigator
        print("Investigator:")
        print(f"  Root cause: {inv.primary_root_cause}")
        for entry in inv.root_cause_breakdown:
            print(f"    {entry.classification}: {entry.claim_count} claims ({entry.pct:.1f}%) — {entry.note}")
        print(f"  Affected branch: {', '.join(inv.affected_branch) or '(none)'}")
        if inv.datasets_checked_and_clean:
            print(f"    (checked, clean, no action needed: {', '.join(inv.datasets_checked_and_clean)})")
        # The repo owner's explicit Slice 4 ask: the lineage path
        # Investigator actually walked, as its own line — §4.4's
        # illustrative example doesn't show this field on its own line;
        # added here because a "printed narrative naming the root cause"
        # is incomplete without also showing WHAT WAS WALKED to reach it.
        print(f"  Lineage path walked: {', '.join(inv.lineage_path_walked) or '(none recorded)'}")
        print(f"  Confidence: {inv.confidence}")
        print(f"  Backend: {inv.backend_used}")
        print()

    if incident.scribe is not None:
        print("Scribe:")
        if not incident.scribe.entities:
            print("  Nothing to write back (no affected entities).")
        for er in incident.scribe.entities:
            if er.entity_urn is None:
                print(f"  {er.entity_name}: skipped — {er.skipped_reason}")
                continue
            tag = "tag (already present)" if er.tag_already_present else "tag" if er.tag_applied else "tag: not applied"
            doc = "doc note (already present)" if er.doc_note_already_present else "doc note" if er.doc_note_added else "doc note: not added"
            if er.assertion_run_event_emitted:
                assertion = "assertion (already defined)" if er.assertion_already_defined else "assertion"
            else:
                assertion = f"assertion: skipped ({er.skipped_reason})"
            print(f"  {er.entity_name}: {tag}, {doc}, {assertion}")
        print()

    if incident.remediator is not None:
        print("Remediator:")
        rem = incident.remediator
        if rem.status == "no_fix_available":
            print(f"  No fix available: {rem.reason}")
        elif rem.status == "failed_validation":
            print(f"  Fix generation FAILED after {len(rem.attempts)} attempt(s): {rem.reason}")
        else:
            already = " (already existed)" if rem.pr_already_existed else ""
            print(f"  PR opened{already}: {rem.pr_url}")
            if rem.fix_target is not None:
                print(f"  Fix target: {rem.fix_target.transform_file}")
            if rem.owner:
                print(f"  Suggested owner: {rem.owner}")
        print()

    if incident.drift is not None:
        print("Drift (feature health check):")
        d = incident.drift
        print(f"  Check: {d.check_id}  |  model_version: {d.model_version}  |  {d.overall_status}")
        for c in d.feature_checks:
            print(f"  {c.feature_name} ({c.check_type}): {c.status} — expected {c.documented_expected}")
        print()

    cost = incident.cost
    cost_str = f"${cost.investigator_cost_usd:.4f}" if cost.investigator_cost_usd is not None else "n/a"
    print(f"Cost: {cost_str}  |  Wall clock: {cost.wall_clock_seconds:.1f}s")
    if written_path is not None:
        print(f"Written: {written_path}")


def print_dry_run_summary(incidents: list[Incident], forced_segment: Optional[Segment]) -> None:
    """§4.3's --dry-run: "print what WOULD happen, spend zero LLM budget."
    No InvestigatorFinding exists for any of these (dry run never
    investigates), so this only ever reports Sentinel's own math plus which
    segments WOULD have gone on to Investigator — read directly from
    `sentinel.flagged` / the forced segment, not from `Incident.status`
    (every Incident here is "no_anomaly" regardless, since none were
    actually investigated in dry-run mode — see run_guardian()'s docstring
    for why that's the honest status value even for a flagged-but-skipped
    segment).
    """
    print(f"Guardian dry run — Sentinel scanned {len(incidents)} segments, spent $0 (no LLM calls).")
    print()

    would_investigate = [
        inc
        for inc in incidents
        if inc.sentinel.flagged or (forced_segment is not None and inc.sentinel.segment == forced_segment)
    ]
    if not would_investigate:
        print("No anomaly detected this run.")
        return

    print(f"Would run Investigator on {len(would_investigate)} segment(s):")
    for inc in would_investigate:
        s = inc.sentinel
        forced_note = " (forced via --segment)" if forced_segment is not None and s.segment == forced_segment else ""
        print(
            f"  {s.segment.insurance_provider} / {s.segment.medical_condition}{forced_note}: "
            f"z = {s.z_score:.2f} (threshold {s.threshold}), "
            f"{s.segment_denial_rate:.1%} vs. {s.baseline_denial_rate:.1%} baseline"
        )
