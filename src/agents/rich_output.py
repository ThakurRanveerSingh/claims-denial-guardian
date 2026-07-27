#!/usr/bin/env python3
"""
rich_output.py — optional rich-terminal presentation for `guardian run`.
Design: docs/decisions/0009-reporter-design.md (Sprint 3 WP3 Part A).

`rich` is an OPTIONAL dependency (`pip install -e ".[rich]"`), not a hard
one — the whole `import rich...` block below is inside a try/except
ImportError, and `RICH_AVAILABLE` is the one flag `cli.py` checks before
ever calling anything in this module. Falls back to orchestrator.py's own
`print_incident_summary()`/`print_dry_run_summary()` when `rich` isn't
installed — the ACTUAL tested, always-available baseline this project has
had since Sprint 2; this module only ever adds presentation on top of
that, never a new source of truth or a code path a plain-text run doesn't
already exercise. Verified directly: this file was written and its
fallback exercised for real in an environment where `rich` genuinely
wasn't installed, not merely guarded in theory.

Colors are meaningful, not decorative — the SAME vocabulary
`src/agents/reporter.py`'s HTML output already uses (its CSS custom
properties share these exact names), so a viewer moving between the
terminal and a generated report doesn't have to relearn what a color
means: red = flagged/implicated, green = cleared, amber = quarantine
(needs human review, decision 0008's own operational-note thesis).

Terminal output stays a compact DASHBOARD — one panel per incident, a
handful of short lines, key numbers only. Full detail (evidence tables,
generated SQL, PR bodies) lives in the report files Reporter already
writes, not crammed into a terminal cell at a video-readable 18pt font
size. This is a deliberate scope boundary, not a limitation of `rich`
itself: a denser view would defeat the actual reason this exists (readable
on camera), so it isn't built even though `rich` could support it.
"""

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

STYLE_FLAGGED = "bold red"
STYLE_CLEARED = "bold green"
STYLE_QUARANTINE = "bold yellow"  # "amber" -- rich has no literal amber, yellow renders closest in most terminal themes
STYLE_DIM = "dim"


def _console() -> "Console":
    return Console()


def print_incident_summary_rich(incident, written_path=None) -> None:
    """Rich-panel equivalent of orchestrator.print_incident_summary() —
    same information, same section order, colorized. Never called unless
    `RICH_AVAILABLE` is True (checked by the caller, cli.py)."""
    console = _console()
    s = incident.sentinel
    body = Text()

    flagged_style = STYLE_FLAGGED if s.flagged else STYLE_CLEARED
    body.append("Sentinel  ", style="bold")
    body.append(f"{s.segment.insurance_provider} / {s.segment.medical_condition}  ")
    body.append(f"z={s.z_score:.2f} (threshold {s.threshold})  ")
    body.append("FLAGGED" if s.flagged else "not flagged", style=flagged_style)
    body.append(f"\n          {s.segment_denial_rate:.1%} ({s.segment_denial_count}/{s.segment_claim_count}) vs. {s.baseline_denial_rate:.1%} baseline\n\n", style=STYLE_DIM)

    if incident.investigator is not None:
        inv = incident.investigator
        body.append("Investigator  ", style="bold")
        body.append(f"{inv.primary_root_cause}  ")
        body.append(f"(confidence: {inv.confidence})\n")
        if inv.affected_branch:
            body.append("  implicated: ", style=STYLE_DIM)
            body.append(", ".join(inv.affected_branch) + "\n", style=STYLE_FLAGGED)
        if inv.datasets_checked_and_clean:
            body.append("  cleared:    ", style=STYLE_DIM)
            body.append(", ".join(inv.datasets_checked_and_clean) + "\n", style=STYLE_CLEARED)
        body.append("\n")

    if incident.scribe is not None:
        body.append("Scribe  ", style="bold")
        if not incident.scribe.entities:
            body.append("nothing to write back\n", style=STYLE_DIM)
        else:
            names = [e.entity_name for e in incident.scribe.entities if e.entity_urn is not None]
            body.append(f"tag + doc note applied to {len(names)} entities: {', '.join(names)}\n")
        body.append("\n")

    if incident.remediator is not None:
        rem = incident.remediator
        body.append("Remediator  ", style="bold")
        if rem.status == "no_fix_available":
            body.append(f"no fix available — {rem.reason}\n", style=STYLE_DIM)
        elif rem.status == "failed_validation":
            body.append(f"FAILED after {len(rem.attempts)} attempt(s) — {rem.reason}\n", style=STYLE_FLAGGED)
        else:
            already = " (already existed)" if rem.pr_already_existed else ""
            body.append(f"PR opened{already}: {rem.pr_url}\n")
            if rem.attempts and rem.attempts[-1].validation.quarantine_count:
                qc = rem.attempts[-1].validation.quarantine_count
                body.append(f"  {qc} rows quarantined", style=STYLE_QUARANTINE)
                body.append(f" — suggested owner: {rem.owner or 'unknown'}\n")
        body.append("\n")

    if "report" in incident.pipeline_stages_run:
        body.append("Report  ", style="bold")
        body.append("examples/" + incident.incident_id + "/report/audit_report.{md,html}\n", style=STYLE_DIM)

    cost = incident.cost
    cost_str = f"${cost.investigator_cost_usd:.4f}" if cost.investigator_cost_usd is not None else "n/a"
    footer = Text(f"\nCost: {cost_str}  |  Wall clock: {cost.wall_clock_seconds:.1f}s", style=STYLE_DIM)
    if written_path is not None:
        footer.append(f"\nWritten: {written_path}", style=STYLE_DIM)
    body.append(footer)

    panel_style = STYLE_FLAGGED if s.flagged else STYLE_CLEARED
    console.print(Panel(body, title=incident.incident_id, border_style=panel_style, expand=False))


def print_dry_run_summary_rich(incidents, forced_segment=None) -> None:
    """Rich-table equivalent of orchestrator.print_dry_run_summary()."""
    console = _console()
    console.print(f"Guardian dry run — Sentinel scanned {len(incidents)} segments, spent $0 (no LLM calls).\n")

    would_investigate = [
        inc for inc in incidents
        if inc.sentinel.flagged or (forced_segment is not None and inc.sentinel.segment == forced_segment)
    ]
    if not would_investigate:
        console.print("No anomaly detected this run.", style=STYLE_CLEARED)
        return

    table = Table(title=f"Would run Investigator on {len(would_investigate)} segment(s)")
    table.add_column("Segment", style="bold")
    table.add_column("Denial rate")
    table.add_column("Baseline")
    table.add_column("z-score", justify="right")

    for inc in would_investigate:
        s = inc.sentinel
        forced_note = " (forced)" if forced_segment is not None and s.segment == forced_segment else ""
        table.add_row(
            f"{s.segment.insurance_provider} / {s.segment.medical_condition}{forced_note}",
            f"{s.segment_denial_rate:.1%}",
            f"{s.baseline_denial_rate:.1%}",
            Text(f"{s.z_score:.2f}", style=STYLE_FLAGGED),
        )
    console.print(table)
