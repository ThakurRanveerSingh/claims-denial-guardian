#!/usr/bin/env python3
"""
`guardian run` — the CLI entrypoint, §4.3.

Installed via `pip install -e .` + this repo's new root-level `pyproject.toml`
(`[project.scripts] guardian = "agents.cli:main"`) — a real `guardian`
command on PATH, not `python -m src.agents.cli run` typed out every time.
See pyproject.toml's own comment for why this mechanism was chosen over a
`bin/guardian` shell shim (§4.3's own rejected-alternative reasoning).
"""

import argparse
import sys
from typing import Optional

from agents.llm_backend import LLMBackendError
from agents.orchestrator import (
    EXAMPLES_DIR,
    Segment,
    print_dry_run_summary,
    print_incident_summary,
    resume_incident,
    run_guardian,
    write_incident,
)
from agents.rich_output import RICH_AVAILABLE, print_dry_run_summary_rich, print_incident_summary_rich


def _parse_segment(value: str) -> tuple[str, str]:
    parts = value.split(",", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise argparse.ArgumentTypeError(f"--segment must be 'PROVIDER,CONDITION' (got {value!r})")
    return (parts[0].strip(), parts[1].strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guardian", description="Claims Denial Guardian — Sentinel/Investigator pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run Sentinel across all segments, then Investigator on any flagged")
    run_parser.add_argument(
        "--segment",
        type=_parse_segment,
        default=None,
        metavar="PROVIDER,CONDITION",
        help="Force-run Investigator against one specific segment, regardless of Sentinel's flag.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run Sentinel only; print what would happen. Spends zero LLM budget.",
    )
    run_parser.add_argument(
        "--no-writeback",
        action="store_true",
        help="Skip Scribe (tag/documentation/assertion writeback to DataHub) after Investigator.",
    )
    run_parser.add_argument(
        "--remediate",
        action="store_true",
        help="Run Remediator after Scribe: generate a fix, validate it against a scratch copy of "
        "healthcare.db, and open a real pull request on denial-guardian-data-platform. Off by "
        "default -- opening a PR is a side effect with its own explicit-consent story.",
    )
    run_parser.add_argument(
        "--llm-backend",
        default=None,
        metavar="claude_code|anthropic|ollama",
        help="Override LLM_BACKEND (.env) for this run only.",
    )
    run_parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help="Override INVESTIGATOR_MAX_BUDGET_USD (.env) for this run only.",
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a previously saved incident from a given stage onward, without re-running "
        "Sentinel/Investigator (which would mint a new incident_id and therefore a new Remediator "
        "PR instead of updating the existing one).",
    )
    resume_parser.add_argument("incident_id", help="An existing examples/<incident_id>/incident.json to resume.")
    resume_parser.add_argument(
        "--stage",
        required=True,
        choices=["remediate"],
        help="Which downstream stage to (re-)run against the saved incident.",
    )
    resume_parser.add_argument(
        "--llm-backend",
        default=None,
        metavar="claude_code|anthropic|ollama",
        help="Override LLM_BACKEND (.env) for this run only.",
    )

    return parser


def _run_command(args: argparse.Namespace) -> int:
    try:
        incidents = run_guardian(
            llm_backend_name=args.llm_backend,
            segment=args.segment,
            dry_run=args.dry_run,
            writeback=not args.no_writeback,
            remediate=args.remediate,
            max_budget_usd=args.max_budget_usd,
        )
    except ValueError as e:
        # A real usage error (e.g. --segment naming something that doesn't
        # exist in the data) — distinct from "ran fine, found nothing"
        # (§4.3's exit-code rule): this is the one case in this function
        # that's actually the CALLER's mistake, not an operational failure
        # of the pipeline itself, but still needs a non-zero exit so
        # scripting around this CLI can tell the two apart.
        print(f"guardian run: {e}", file=sys.stderr)
        return 2
    except LLMBackendError as e:
        # LLD §6 failure mode 2/similar: the configured backend genuinely
        # can't run at all (CLI missing, API key missing/rejected) — fails
        # here, at the top level, rather than mid-pipeline, since
        # get_backend() is only ever called lazily inside run_guardian()
        # the moment an investigation is actually about to happen (dry runs
        # and clean runs never hit this at all).
        print(f"guardian run: backend unavailable — {e}", file=sys.stderr)
        return 1

    if args.dry_run:
        forced = Segment(*args.segment) if args.segment else None
        if RICH_AVAILABLE:
            print_dry_run_summary_rich(incidents, forced_segment=forced)
        else:
            print_dry_run_summary(incidents, forced_segment=forced)
        return 0

    interesting = [inc for inc in incidents if inc.status != "no_anomaly"]

    if not interesting:
        # LLD §6 failure mode 4: "no anomaly detected" is a valid, expected
        # outcome, not a failure — exit 0, plain message, no file written.
        print("No anomaly detected this run.")
        return 0

    for incident in interesting:
        written_path = write_incident(incident, examples_dir=EXAMPLES_DIR)
        if RICH_AVAILABLE:
            # print_incident_summary_rich() already includes the report
            # path line itself (see its own "Report" section) -- the
            # plain-text path below prints it separately since
            # print_incident_summary() predates Reporter and isn't itself
            # modified for this.
            print_incident_summary_rich(incident, written_path=written_path)
        else:
            print_incident_summary(incident, written_path=written_path)
            if "report" in incident.pipeline_stages_run:
                # write_audit_reports()'s own output location is fully
                # deterministic (examples/<incident_id>/report/...) --
                # printed here rather than threading the real return paths
                # back out of run_guardian()'s loop, which would mean
                # adding a field to Incident/incident.json just to carry
                # two paths a caller can already derive from the
                # incident_id alone.
                report_dir = EXAMPLES_DIR / incident.incident_id / "report"
                print(f"Report: {report_dir / 'audit_report.md'}")
                print(f"        {report_dir / 'audit_report.html'}")
            print()

    return 0


def _resume_command(args: argparse.Namespace) -> int:
    try:
        incident = resume_incident(args.incident_id, args.stage, llm_backend_name=args.llm_backend)
    except (ValueError, FileNotFoundError) as e:
        # A real usage error (unknown incident_id, unsupported stage, no
        # InvestigatorFinding to resume from) -- same exit-code convention
        # as `guardian run`'s own ValueError handling.
        print(f"guardian resume: {e}", file=sys.stderr)
        return 2
    except LLMBackendError as e:
        print(f"guardian resume: backend unavailable — {e}", file=sys.stderr)
        return 1

    written_path = write_incident(incident, examples_dir=EXAMPLES_DIR)
    print_incident_summary(incident, written_path=written_path)
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return _run_command(args)
    if args.command == "resume":
        return _resume_command(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
