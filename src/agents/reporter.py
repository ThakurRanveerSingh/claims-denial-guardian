#!/usr/bin/env python3
"""
Reporter — generates the audit report (US-5): a compliance-officer-readable
Markdown document and a self-contained HTML page, from a completed
`Incident`. Design: docs/decisions/0009-reporter-design.md.

Zero LLM calls, fully deterministic — the same "LLM proposes, code
verifies" boundary this project keeps everywhere else, just with no LLM
present in this module at all: there's nothing here that benefits from
judgment, only from correctly rendering decisions earlier stages already
made. Two live, read-only SQL queries are the one place this module does
real work at generation time: the member-impact breakdown and the raw
counts behind the z-test are both re-derived fresh from the database, not
merely echoed from `Incident`'s own cached JSON fields — so the audit
document is independently verifiable against the database itself, not
just trusting Sentinel's/Investigator's already-summarized numbers.

Templating: stdlib `string.Template` + manual `html.escape()` in the
handful of list-driven table loops, not Jinja2. Jinja2's autoescaping and
native `{% for %}` loops are real, honest advantages for the HTML side —
rejected anyway to stay consistent with how every other deterministic
template in this codebase (Scribe's doc-note text, Remediator's PR body)
is built, for a templating need (a fixed handful of sections, bounded list
iteration) that doesn't need a real templating engine's loop/inheritance
machinery to stay correct and simple. Recorded in decision 0009, not
re-litigated here.

`generated_at` is a REAL wall-clock timestamp in both outputs, by explicit
instruction — real information belongs in the report; determinism is
proven by the golden-file test normalizing/excluding that one named field
when diffing (tests/test_reporter.py), not by omitting real information
from the content.

UAT amendment (Sprint 3 WP3 Part C squint test): the first version of this
module put raw agent telemetry — Python function names, source file
paths, literal SQL, `mcp__datahub__*` tool-call syntax — directly into the
sections a compliance officer is meant to read standalone. Reading it cold
as a non-technical reviewer (the repo owner's own explicit UAT method,
not just re-inspecting the code) surfaced this directly. Fixed by keeping
the raw evidence trail — genuinely valuable for reproducibility — but
moving it to a clearly labeled Technical Appendix at the end of both
formats, and rewriting `_detection_narrative()`/`_investigation_narrative()`
so a compliance reader never has to see a function name, file path, or a
structured/coded tag (e.g. `introduced_at:claims`) to follow sections 1-3.
"""

import html
import re
import sqlite3
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.sentinel import Segment, load_segment_counts

_MODULE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _MODULE_DIR.parent.parent
TEMPLATES_DIR = _MODULE_DIR / "templates"

# Same absolute-path convention every other agent module in this repo uses.
DB_PATH = (_REPO_ROOT / "src" / "datahub" / "healthcare.db").resolve()
EXAMPLES_DIR = (_REPO_ROOT / "examples").resolve()

MAIN_REPO_GITHUB_SLUG = "ThakurRanveerSingh/claims-denial-guardian"

# The pipeline's one fixed, known topology (same table names, same order
# src/codegen/fresh_build_validation.py's TRANSFORM_ORDER already encodes,
# with raw_patients — the one upstream source outside that module's own
# scope — added at the front). Used ONLY for the lineage diagram's node
# sequence, NOT for `InvestigatorFinding.lineage_path_walked` itself: that
# field is free-text narration of the TOOL CALLS Investigator made (e.g.
# "get_lineage(upstream, urn=claims, max_hops=3) -> mart_billing (degree
# 1), ..."), not a clean ordered list of bare node names — checked
# directly against a real saved incident, not assumed. A diagram driven by
# membership-testing that narration against `affected_branch`/
# `datasets_checked_and_clean` (both bare table names) would never match
# anything and color every node the same "unexamined" gray. The fixed
# topology below is simple (mart_billing/mart_demographics are really a
# parallel fork off staging_patients, not sequential — flattened into one
# row anyway, since Part A explicitly asked for "a simple left-to-right
# diagram", not an exact dependency graph) and correct: it's the same five
# tables this project's own pipeline has always had.
PIPELINE_TOPOLOGY = ["raw_patients", "staging_patients", "mart_billing", "mart_demographics", "claims"]


# ---------------------------------------------------------------------------
# Severity — z-score magnitude, named explicitly, not guessed at per report.
# ---------------------------------------------------------------------------


def severity_for(z_score: float) -> str:
    """A plain, stated bucketing: z >= 20 "Critical", z >= 10 "High",
    otherwise "Moderate" (the floor here is meaningful because a report is
    only ever generated for a flagged/investigated segment, i.e. z already
    cleared Sentinel's own threshold — "Moderate" still means "real and
    already investigated," not "borderline")."""
    if z_score >= 20:
        return "Critical"
    if z_score >= 10:
        return "High"
    return "Moderate"


def _severity_css_class(severity: str) -> str:
    return f"severity-{severity.lower()}"


# ---------------------------------------------------------------------------
# Live db context — member impact + the z-test's own raw counts, both
# re-derived fresh at report-generation time.
# ---------------------------------------------------------------------------


@dataclass
class BaselineContext:
    """The four raw counts `two_proportion_z_test()`
    (src/agents/sentinel.py) itself takes as input — presented so a reader
    with a calculator can recompute the z-score by hand from the SAME
    numbers Sentinel used, not merely read `SentinelFinding`'s already-
    summarized rate and take it on faith."""

    segment_claims: int
    segment_denials: int
    rest_claims: int
    rest_denials: int


def load_baseline_context(conn: sqlite3.Connection, segment: Segment) -> BaselineContext:
    counts = load_segment_counts(conn)
    if segment not in counts:
        raise ValueError(f"segment {segment!r} not found in the current claims/denials data")
    segment_claims, segment_denials = counts[segment]
    total_claims = sum(c for c, _ in counts.values())
    total_denials = sum(d for _, d in counts.values())
    return BaselineContext(
        segment_claims=segment_claims, segment_denials=segment_denials,
        rest_claims=total_claims - segment_claims, rest_denials=total_denials - segment_denials,
    )


@dataclass
class MemberImpactRow:
    category: str
    claim_count: int


def load_member_impact(conn: sqlite3.Connection, segment: Segment) -> list:
    """Denial counts by reason code for THIS segment specifically — a live
    GROUP BY query against the same `claims`/`denials` tables (and the same
    fixed, version-controlled column names) `sentinel.load_segment_counts()`
    already reads directly; see that function's own docstring for why this
    doesn't go through a live DataHub schema check either."""
    rows = conn.execute(
        """
        SELECT d.denial_reason_code, COUNT(*) AS claim_count
        FROM claims c
        JOIN denials d ON d.claim_id = c.claim_id
        WHERE c.insurance_provider = ? AND c.medical_condition = ?
        GROUP BY d.denial_reason_code
        ORDER BY claim_count DESC
        """,
        (segment.insurance_provider, segment.medical_condition),
    ).fetchall()
    return [MemberImpactRow(category=reason, claim_count=count) for reason, count in rows]


# ---------------------------------------------------------------------------
# GitHub link — same "read the real git remote, never hardcode" discipline
# decision 0007/0008 already established for Scribe/Remediator, duplicated
# here rather than importing a private helper across modules (same "small
# per-module boilerplate, copied not shared" convention).
# ---------------------------------------------------------------------------


def _incident_github_url(incident_id: str) -> str:
    return f"https://github.com/{MAIN_REPO_GITHUB_SLUG}/blob/main/examples/{incident_id}/incident.json"


# ---------------------------------------------------------------------------
# Shared narrative sections (format-agnostic content, rendered per-format
# below by the two _*_md / _*_html helper families).
# ---------------------------------------------------------------------------


def _detection_narrative(sentinel, baseline: BaselineContext) -> str:
    """Plain prose only — no function names, file paths, or raw SQL (Part C
    UAT amendment). The exact statistical method and formula still exist
    in the report; they live in the Technical Appendix, not here."""
    s = sentinel
    return (
        f"Sentinel flagged {s.segment.insurance_provider} / {s.segment.medical_condition} for a denial "
        f"rate of {s.segment_denial_rate:.1%} ({s.segment_denial_count} of {s.segment_claim_count} claims), "
        f"compared to a baseline of {s.baseline_denial_rate:.1%} built from every other segment "
        f"({baseline.rest_denials} of {baseline.rest_claims} claims) — this segment is excluded from its "
        f"own baseline, so a real spike here can't inflate the very baseline it's measured against. This "
        f"is a statistically significant deviation: a standard two-proportion statistical test comparing "
        f"this segment's rate to the baseline produces a z-score of {s.z_score:.2f}, well above the "
        f"flagging threshold of {s.threshold}. The four counts behind this calculation — this segment's "
        f"claims and denials, and the baseline's claims and denials — are {baseline.segment_claims} and "
        f"{baseline.segment_denials} (segment), {baseline.rest_claims} and {baseline.rest_denials} "
        f"(baseline), stated here so the result can be independently recomputed. The exact calculation "
        f"method is documented in the Technical Appendix below."
    )


def _plain_root_cause(primary_root_cause: str) -> str:
    """Translates the structured, colon-separated primary_root_cause tag
    (e.g. "introduced_at:claims") into a plain-English phrase — the
    compliance-facing narrative must not show this coded format directly
    (Part C UAT amendment)."""
    if primary_root_cause.startswith("introduced_at:"):
        table = primary_root_cause.split(":", 1)[1]
        return f"a defect introduced during the {table} build process itself"
    if primary_root_cause.startswith("inherited_from:"):
        table = primary_root_cause.split(":", 1)[1]
        return f"a pre-existing issue inherited from {table}, the original source data"
    if primary_root_cause == "inconclusive":
        return "a cause the investigation could not conclusively determine"
    return primary_root_cause  # unknown shape -- show as-is rather than hide information


def _investigation_narrative(finding) -> str:
    """Plain prose only (Part C UAT amendment) — root_cause_summary is
    Investigator's own narrative (already prose, kept verbatim: rewriting
    it would mean editorializing another agent's finding, the same "don't
    paraphrase what another stage already concluded" discipline
    Remediator's PR body applies to this exact field). The RAW lineage
    trace (tool-call syntax, `get_lineage(...)`) is deliberately NOT shown
    here — it moved to the Technical Appendix."""
    if finding is None:
        return "No investigation was performed (Sentinel did not flag this segment)."
    lines = [
        f"Root cause: {_plain_root_cause(finding.primary_root_cause)}. Confidence: {finding.confidence}.",
        "",
        finding.root_cause_summary,
    ]
    if finding.datasets_checked_and_clean:
        lines.append("")
        lines.append(f"Also checked and confirmed clean, with no defect found: {', '.join(finding.datasets_checked_and_clean)}.")
    lines.append("")
    lines.append(
        "The complete technical trace behind this conclusion — every query run and every check "
        "performed — is in the Technical Appendix below."
    )
    return "\n".join(lines)


def _plain_classification(classification: str) -> str:
    """Root-cause-breakdown entries look like "introduced_at:claims
    (sign-flip bug in claims build from mart_billing)" or
    "no_known_data_quality_hypothesis:HIGH_RISK_SCORE" (the exact prefix
    genuinely varies between real investigations — checked directly
    against both real saved incidents, not assumed to be one fixed
    string). Strips the coded tag, keeps the plain-language parenthetical
    when there is one, and falls back to a readable phrase built from the
    tag itself when there isn't (Part C UAT amendment)."""
    paren_match = re.match(r"^\S+:\S+\s*\((.+)\)$", classification)
    if paren_match:
        text = paren_match.group(1)
        return text[0].upper() + text[1:]
    if classification.startswith("introduced_at:"):
        return f"Introduced during the {classification.split(':', 1)[1]} build"
    if classification.startswith("inherited_from:"):
        return f"Inherited from {classification.split(':', 1)[1]} (original source data)"
    if "hypothesis" in classification and ":" in classification:
        reason = classification.split(":", 1)[1]
        return f"{reason.replace('_', ' ').title()} (no data-quality hypothesis to trace)"
    return classification  # unknown shape -- show as-is rather than hide information


def _actions_taken_lines(incident) -> list:
    """Returns a list of plain-text lines (no markup) — each render format
    wraps/escapes these itself. Empty list means nothing has happened yet
    (both render functions turn that into an honest "nothing yet" message,
    not a blank section).

    Tag/doc wording distinguishes "already present" from "applied/added
    this run" — same three-way wording orchestrator.print_incident_summary()
    already uses correctly. An earlier version of this function collapsed
    both into one word ("applied"/"added"), which meant the report's own
    claim couldn't actually show the real, sometimes-mixed idempotency
    result (e.g. a tag shared with an earlier incident showing
    already-present while that same run's documentation note is still
    genuinely new) — caught during Part C UAT re-verification by checking
    the report text against what the live Scribe run had actually just
    printed, not by re-reading the code.
    """
    lines = []
    if incident.scribe is not None and incident.scribe.entities:
        lines.append("DataHub writeback (Scribe):")
        for e in incident.scribe.entities:
            if e.entity_urn is None:
                continue
            if e.tag_already_present:
                tag = "tag already present"
            elif e.tag_applied:
                tag = "tag applied"
            else:
                tag = "tag not applied"
            if e.doc_note_already_present:
                doc = "documentation note already present"
            elif e.doc_note_added:
                doc = "documentation note added"
            else:
                doc = "documentation note not added"
            lines.append(f"  {e.entity_name}: {tag}, {doc}")
        if incident.scribe.doc_url:
            lines.append(f"  Documentation link: {incident.scribe.doc_url}")
    if incident.remediator is not None and incident.remediator.status == "success" and incident.remediator.pr_url:
        lines.append("Fix opened (Remediator):")
        lines.append(f"  Pull request: {incident.remediator.pr_url}")
        if incident.remediator.fix_target is not None:
            lines.append(f"  File changed: {incident.remediator.fix_target.transform_file}")
        if incident.remediator.attempts:
            v = incident.remediator.attempts[-1].validation
            conservation = "PASS" if v.conserves_rows else "FAIL"
            lines.append(
                f"  Validation: {v.quarantine_count} rows quarantined, {v.violation_count_in_clean} "
                f"violations remaining (must be 0), conservation {conservation}"
            )
    return lines


def _outstanding_items_text(incident) -> str:
    if incident.remediator is None or incident.remediator.status != "success":
        return "No fix has been generated yet — outstanding items will be determined once Remediator runs."
    quarantine_table = f"{incident.remediator.fix_target.table_name}_quarantine" if incident.remediator.fix_target else "the quarantine table"
    owner = incident.remediator.owner or "unknown — not found in DataHub ownership metadata"
    if incident.remediator.attempts:
        quarantine_count = incident.remediator.attempts[-1].validation.quarantine_count
        return f"{quarantine_count} rows in {quarantine_table} require human review — suggested owner: {owner}."
    # attempts is empty when this RemediatorResult came from the idempotency
    # short-circuit (an already-open PR was found; see the PR itself for
    # the real count) — still real, useful information, just without a
    # freshly computed number to quote here.
    return f"Rows in {quarantine_table} (see the PR above for the exact count) require human review — suggested owner: {owner}."


# ---------------------------------------------------------------------------
# Markdown rendering.
# ---------------------------------------------------------------------------


def _md_table(headers: list, rows: list) -> str:
    """`rows`: list of tuples of already-string-safe cell values (pipe
    characters escaped by the caller where the source is free text)."""
    if not rows:
        return "_(none)_"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _escape_md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _evidence_table_md(finding) -> str:
    if finding is None or not finding.evidence:
        return "_(no investigation was performed)_"
    rows = [
        (_escape_md_cell(e.step), _escape_md_cell(e.tool), _escape_md_cell(e.query_or_call), _escape_md_cell(e.result_summary))
        for e in finding.evidence
    ]
    return _md_table(["Step", "Tool", "Query/Call", "Result"], rows)


def _breakdown_table_md(finding) -> str:
    """Classification column uses _plain_classification() (Part C UAT
    amendment) — the raw coded tag never appears in the main body; the
    Technical Appendix's evidence table still has the full raw data for
    anyone who wants it."""
    if finding is None or not finding.root_cause_breakdown:
        return "_(no investigation was performed)_"
    rows = [
        (_escape_md_cell(_plain_classification(e.classification)), str(e.claim_count), f"{e.pct:.1f}%", _escape_md_cell(e.note))
        for e in finding.root_cause_breakdown
    ]
    return _md_table(["Classification", "Claims", "%", "Note"], rows)


def _member_impact_table_md(rows: list) -> str:
    if not rows:
        return "_(no denials recorded for this segment)_"
    return _md_table(["Denial reason", "Claims"], [(_escape_md_cell(r.category), str(r.claim_count)) for r in rows])


def _technical_appendix_md(finding) -> str:
    """Everything a compliance reader does NOT need: the raw lineage
    trace (tool-call syntax) and the full evidence log (literal SQL, MCP
    tool names). Kept verbatim, per the repo owner's explicit instruction
    — reproducibility matters, it just doesn't belong in the sections a
    non-technical reviewer reads first (Part C UAT amendment)."""
    if finding is None:
        return "_(no investigation was performed)_"
    lineage = " -> ".join(finding.lineage_path_walked) if finding.lineage_path_walked else "(none recorded)"
    return (
        "This section is the raw technical trace Investigator used to reach its conclusion — "
        "included for reproducibility and engineering review. Compliance readers do not need to "
        "read this section.\n\n"
        "**Statistical method**: `two_proportion_z_test()` in `src/agents/sentinel.py` "
        "(two-proportion z-test, leave-one-out baseline).\n\n"
        f"**Raw lineage trace**: {_escape_md_cell(lineage)}\n\n"
        "**Evidence log**:\n\n"
        f"{_evidence_table_md(finding)}"
    )


def generate_audit_report_md(incident, *, healthcare_db_path: Path = DB_PATH, generated_at: Optional[datetime] = None) -> str:
    """Renders `templates/audit_report.md.tmpl` for `incident`. `generated_at`
    defaults to the real wall-clock time; tests may pass a fixed value, but
    the golden-file determinism test itself does NOT rely on that — see
    tests/test_reporter.py's own docstring for why (the repo owner's
    explicit call: real information belongs in the report, the test
    normalizes the one non-deterministic field instead)."""
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)

    conn = sqlite3.connect(f"file:{healthcare_db_path}?mode=ro", uri=True)
    try:
        baseline = load_baseline_context(conn, incident.sentinel.segment)
        member_impact = load_member_impact(conn, incident.sentinel.segment)
    finally:
        conn.close()

    action_lines = _actions_taken_lines(incident)
    actions_taken = "\n".join(action_lines) if action_lines else "No writeback or remediation has been performed for this incident yet."

    template = string.Template((TEMPLATES_DIR / "audit_report.md.tmpl").read_text())
    return template.substitute(
        incident_id=incident.incident_id,
        status=incident.status,
        severity=severity_for(incident.sentinel.z_score),
        generated_at=generated_at.isoformat(),
        detection_narrative=_detection_narrative(incident.sentinel, baseline),
        investigation_narrative=_investigation_narrative(incident.investigator),
        breakdown_table=_breakdown_table_md(incident.investigator),
        member_impact_table=_member_impact_table_md(member_impact),
        actions_taken=actions_taken,
        outstanding_items=_outstanding_items_text(incident),
        technical_appendix=_technical_appendix_md(incident.investigator),
        incident_json_url=_incident_github_url(incident.incident_id),
    )


# ---------------------------------------------------------------------------
# HTML rendering.
# ---------------------------------------------------------------------------

_ORIGIN_SPLIT_COLORS = ["#c0392b", "#e67e22", "#7f8c8d", "#8e44ad", "#2980b9", "#16a085"]


def _html_table(headers: list, rows: list) -> str:
    """`rows`: list of tuples of RAW (unescaped) cell values — escaping
    happens here, once, so callers never have to remember to do it
    themselves at each call site."""
    if not rows:
        return "<p><em>(none)</em></p>"
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _evidence_table_html(finding) -> str:
    if finding is None or not finding.evidence:
        return "<p><em>No investigation was performed.</em></p>"
    rows = [(e.step, e.tool, e.query_or_call, e.result_summary) for e in finding.evidence]
    return _html_table(["Step", "Tool", "Query/Call", "Result"], rows)


def _breakdown_table_html(finding) -> str:
    """Classification column uses _plain_classification() (Part C UAT
    amendment) — see _breakdown_table_md()'s own docstring."""
    if finding is None or not finding.root_cause_breakdown:
        return "<p><em>No investigation was performed.</em></p>"
    rows = [(_plain_classification(e.classification), e.claim_count, f"{e.pct:.1f}%", e.note) for e in finding.root_cause_breakdown]
    return _html_table(["Classification", "Claims", "%", "Note"], rows)


def _member_impact_table_html(rows: list) -> str:
    if not rows:
        return "<p><em>No denials recorded for this segment.</em></p>"
    return _html_table(["Denial reason", "Claims"], [(r.category, r.claim_count) for r in rows])


def _technical_appendix_html(finding) -> str:
    """HTML equivalent of _technical_appendix_md() — same content, same
    "compliance readers don't need this" framing (Part C UAT amendment)."""
    if finding is None:
        return "<p><em>No investigation was performed.</em></p>"
    lineage = " &rarr; ".join(html.escape(step) for step in finding.lineage_path_walked) if finding.lineage_path_walked else "(none recorded)"
    return (
        "<p>This section is the raw technical trace Investigator used to reach its conclusion — "
        "included for reproducibility and engineering review. Compliance readers do not need to "
        "read this section.</p>"
        "<p><strong>Statistical method</strong>: <code>two_proportion_z_test()</code> in "
        "<code>src/agents/sentinel.py</code> (two-proportion z-test, leave-one-out baseline).</p>"
        f"<p><strong>Raw lineage trace</strong>: {lineage}</p>"
        "<p><strong>Evidence log</strong>:</p>"
        f"{_evidence_table_html(finding)}"
    )


def _origin_split_html(finding) -> str:
    """Legend/tooltip text uses _plain_classification() (Part C UAT
    amendment) — this diagram is one of the sections a compliance reader
    looks at directly, same reasoning as the breakdown table."""
    if finding is None or not finding.root_cause_breakdown:
        return "<p><em>No investigation was performed.</em></p>"
    total = sum(e.claim_count for e in finding.root_cause_breakdown) or 1
    segments, legend_items = [], []
    for i, e in enumerate(finding.root_cause_breakdown):
        color = _ORIGIN_SPLIT_COLORS[i % len(_ORIGIN_SPLIT_COLORS)]
        pct = e.claim_count / total * 100
        plain = _plain_classification(e.classification)
        title = html.escape(f"{plain}: {e.claim_count} ({e.pct:.1f}%)")
        segments.append(f'<div class="split-segment" style="width:{pct:.2f}%;background:{color}" title="{title}"></div>')
        legend_items.append(
            f'<span class="legend-item"><span class="swatch" style="background:{color}"></span>'
            f"{html.escape(plain)} ({e.claim_count})</span>"
        )
    return f'<div class="split-bar">{"".join(segments)}</div><div class="legend">{"".join(legend_items)}</div>'


def _lineage_diagram_html(finding) -> str:
    """Renders PIPELINE_TOPOLOGY's fixed node sequence, colored by whether
    each table is implicated/cleared/unexamined for THIS finding -- not
    `finding.lineage_path_walked` (free-text tool-call narration, not node
    names; see PIPELINE_TOPOLOGY's own comment for why membership-testing
    against it wouldn't work).

    Prepends a one-line caption ("N of 5 pipeline stages implicated") --
    Part C UAT finding: both real incidents' diagrams are red-dominated
    (3-4 of 5 boxes), so the distinguishing signal between two incidents
    read side by side is real but not the FIRST thing that registers at a
    genuine half-second glance. The caption gives an instant number to
    anchor on before a viewer has to parse which specific boxes differ.
    Computed from the exact same red/green node classification below, not
    a second, separately-derived count that could drift out of sync with
    the colors it's captioning.
    """
    if finding is None:
        return "<p><em>No investigation was performed.</em></p>"
    affected = set(finding.affected_branch)
    cleared = set(finding.datasets_checked_and_clean)
    parts = []
    implicated_count = 0
    for i, node in enumerate(PIPELINE_TOPOLOGY):
        if node in affected:
            css_class = "node-implicated"
            implicated_count += 1
        elif node in cleared:
            css_class = "node-cleared"
        else:
            css_class = "node-unexamined"
        parts.append(f'<div class="lineage-node {css_class}">{html.escape(node)}</div>')
        if i < len(PIPELINE_TOPOLOGY) - 1:
            parts.append('<div class="lineage-arrow">&rarr;</div>')
    legend = (
        '<div class="legend">'
        '<span class="legend-item"><span class="swatch" style="background:var(--critical)"></span>Implicated</span>'
        '<span class="legend-item"><span class="swatch" style="background:var(--cleared)"></span>Cleared (checked, clean)</span>'
        '<span class="legend-item"><span class="swatch" style="background:var(--unexamined)"></span>Not examined</span>'
        "</div>"
    )
    caption = f'<p class="lineage-caption"><strong>{implicated_count} of {len(PIPELINE_TOPOLOGY)} pipeline stages implicated</strong></p>'
    return f'{caption}<div class="lineage-row">{"".join(parts)}</div>{legend}'


def _actions_taken_html(incident) -> str:
    lines = _actions_taken_lines(incident)
    if not lines:
        return "<p>No writeback or remediation has been performed for this incident yet.</p>"
    return "<br>".join(html.escape(line) for line in lines)


def generate_audit_report_html(incident, *, healthcare_db_path: Path = DB_PATH, generated_at: Optional[datetime] = None) -> str:
    """Renders `templates/audit_report.html.tmpl` for `incident`. Same
    generated_at handling as generate_audit_report_md()."""
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)

    conn = sqlite3.connect(f"file:{healthcare_db_path}?mode=ro", uri=True)
    try:
        baseline = load_baseline_context(conn, incident.sentinel.segment)
        member_impact = load_member_impact(conn, incident.sentinel.segment)
    finally:
        conn.close()

    s = incident.sentinel
    max_rate = max(s.segment_denial_rate, s.baseline_denial_rate, 0.001)
    severity = severity_for(s.z_score)

    template = string.Template((TEMPLATES_DIR / "audit_report.html.tmpl").read_text())
    return template.substitute(
        incident_id=html.escape(incident.incident_id),
        status=html.escape(incident.status),
        severity=html.escape(severity),
        severity_css_class=_severity_css_class(severity),
        generated_at=generated_at.isoformat(),
        segment_bar_pct=f"{(s.segment_denial_rate / max_rate) * 100:.1f}",
        baseline_bar_pct=f"{(s.baseline_denial_rate / max_rate) * 100:.1f}",
        segment_rate_display=f"{s.segment_denial_rate:.1%} ({s.segment_denial_count}/{s.segment_claim_count})",
        baseline_rate_display=f"{s.baseline_denial_rate:.1%} ({baseline.rest_denials}/{baseline.rest_claims})",
        detection_narrative_html=html.escape(_detection_narrative(s, baseline)),
        investigation_narrative_html=html.escape(_investigation_narrative(incident.investigator)).replace("\n", "<br>"),
        origin_split_html=_origin_split_html(incident.investigator),
        lineage_diagram_html=_lineage_diagram_html(incident.investigator),
        breakdown_table_html=_breakdown_table_html(incident.investigator),
        member_impact_table_html=_member_impact_table_html(member_impact),
        actions_taken_html=_actions_taken_html(incident),
        outstanding_items_html=html.escape(_outstanding_items_text(incident)),
        technical_appendix_html=_technical_appendix_html(incident.investigator),
        incident_json_url=_incident_github_url(incident.incident_id),
    )


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def write_audit_reports(incident, *, examples_dir: Path = EXAMPLES_DIR, healthcare_db_path: Path = DB_PATH) -> tuple:
    """Generates both formats and writes them to
    examples/<incident_id>/report/{audit_report.md,audit_report.html}.
    Returns (md_path, html_path). No side effects beyond local files — no
    DataHub write, no PR — so this runs unconditionally for any incident
    that reaches it, unlike --remediate/--no-writeback's consent story."""
    report_dir = examples_dir / incident.incident_id / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now(timezone.utc)
    md_path = report_dir / "audit_report.md"
    html_path = report_dir / "audit_report.html"
    md_path.write_text(generate_audit_report_md(incident, healthcare_db_path=healthcare_db_path, generated_at=generated_at))
    html_path.write_text(generate_audit_report_html(incident, healthcare_db_path=healthcare_db_path, generated_at=generated_at))
    return md_path, html_path
