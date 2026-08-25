"""Command-line entry point.

Normal use is a single command::

    python -m jobfinder run

Everything else here exists for maintenance: seeding a board by hand, rebuilding
the reports offline, or checking a few live boards without touching the main
database.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import AppConfig, load_config
from .db import Database
from .experience import label_for as experience_label
from .geo import GEO_TAGS, label_for as geo_label
from . import server as server_module
from .pipeline import (
    add_companies,
    build_source,
    discovery_report,
    format_summary,
    refresh_opportunities,
    rescore_jobs,
    run_pipeline,
    write_reports,
)
from .scoring import analyze_normalized_job
from .sources.base import SourceError


LOGGER = logging.getLogger("jobfinder")


def _configure_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s" if verbose else "%(message)s",
        stream=sys.stderr,
    )
    # Third-party HTTP chatter is noise unless diagnosing a network problem.
    logging.getLogger("urllib3").setLevel(logging.WARNING if verbose else logging.ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobfinder",
        description=(
            "Discover Ashby job boards, rank entry-level software roles, and generate "
            "a local dashboard with direct application links."
        ),
    )
    parser.add_argument("--version", action="version", version=f"SproutJobs {__version__}")
    parser.add_argument(
        "--config",
        default="config.toml",
        help="path to config.toml (default: %(default)s)",
    )
    parser.add_argument("--verbose", action="store_true", help="show detailed diagnostics")
    parser.add_argument("--quiet", action="store_true", help="only report errors")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="run the full pipeline (default command)")
    run_parser.add_argument(
        "--skip-discovery", action="store_true", help="refresh known boards only"
    )
    run_parser.add_argument(
        "--refresh-discovery",
        action="store_true",
        help="re-read discovery sources even when the cache is fresh",
    )
    run_parser.add_argument(
        "--max-companies", type=int, default=None, help="contact at most N boards this run"
    )
    run_parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            "also query the Internet Archive CDX index (~5,000 candidate boards "
            "instead of ~600). Much slower and much larger; see SOURCES.md."
        ),
    )
    run_parser.add_argument(
        "--company",
        action="append",
        default=None,
        metavar="SLUG",
        help="limit the run to specific known boards (repeatable)",
    )
    run_parser.add_argument(
        "--min-score", type=int, default=None, help="override report minimum score (0-100)"
    )
    run_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="pre-select this freshness window in the dashboard (0 = any age)",
    )
    run_parser.add_argument(
        "--include-non-software",
        action="store_true",
        help="also export roles that were not classified as software",
    )
    run_parser.add_argument(
        "--no-report", action="store_true", help="collect only; do not regenerate reports"
    )
    run_parser.add_argument(
        "--open", action="store_true", help="open the dashboard when the run finishes"
    )
    run_parser.add_argument(
        "--serve",
        action="store_true",
        help="start the local dashboard when the run finishes",
    )
    run_parser.add_argument("--host", default=server_module.DEFAULT_HOST)
    run_parser.add_argument("--port", type=int, default=server_module.DEFAULT_PORT)

    def _dashboard_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument("--host", default=server_module.DEFAULT_HOST)
        target.add_argument("--port", type=int, default=server_module.DEFAULT_PORT)
        target.add_argument(
            "--refresh",
            action="store_true",
            help="rebuild the canonical opportunity set before serving",
        )

    serve_parser = subparsers.add_parser(
        "serve", help="run the local dashboard over the complete job set"
    )
    _dashboard_flags(serve_parser)
    serve_parser.add_argument(
        "--open", action="store_true", help="open a browser once the server is up"
    )

    open_parser = subparsers.add_parser(
        "open", help="start the dashboard if needed and open it in a browser"
    )
    _dashboard_flags(open_parser)
    open_parser.add_argument(
        "--static",
        action="store_true",
        help="open the optional capped HTML export instead of the live dashboard",
    )

    report_parser = subparsers.add_parser(
        "report", help="rebuild the dashboard and CSV from the local database"
    )
    report_parser.add_argument("--min-score", type=int, default=None)
    report_parser.add_argument("--days", type=int, default=None)
    report_parser.add_argument("--include-non-software", action="store_true")

    rescore_parser = subparsers.add_parser(
        "rescore",
        help="re-score stored jobs with the current rules, then rebuild the reports",
    )
    rescore_parser.add_argument("--min-score", type=int, default=None)
    rescore_parser.add_argument("--days", type=int, default=None)
    rescore_parser.add_argument("--include-non-software", action="store_true")

    add_company = subparsers.add_parser("add-company", help="verify and add a board slug")
    add_company.add_argument("slug", nargs="+")

    add_url = subparsers.add_parser("add-url", help="verify and add an Ashby board URL")
    add_url.add_argument("url", nargs="+")

    companies = subparsers.add_parser("companies", help="list the company registry")
    companies.add_argument(
        "--all", action="store_true", help="include unverified and inactive boards"
    )
    companies.add_argument("--limit", type=int, default=50)

    subparsers.add_parser(
        "discovery-report",
        help="show where every candidate board slug came from, and the overlap",
    )

    subparsers.add_parser(
        "stats",
        help="show geography and experience-evidence counts for stored jobs",
    )

    compliance_parser = subparsers.add_parser(
        "compliance",
        help="audit this checkout's engineering safeguards (not a legal opinion)",
    )
    compliance_parser.add_argument(
        "--json", action="store_true", help="emit the audit as JSON instead of text"
    )

    smoke = subparsers.add_parser(
        "smoke-test",
        help="check live boards against a scratch database (never touches your data)",
    )
    smoke.add_argument("slug", nargs="*", default=["notion", "linear", "ramp"])

    return parser


def _apply_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    min_score = getattr(args, "min_score", None)
    if min_score is not None:
        if not 0 <= min_score <= 100:
            raise ValueError("--min-score must be between 0 and 100")
        config.search.min_score = min_score
    days = getattr(args, "days", None)
    if days is not None:
        if days < 0:
            raise ValueError("--days cannot be negative")
        # Pre-selects the dashboard's freshness filter; 0 means any age.
        config.search.days_to_show = days
    if getattr(args, "include_non_software", False):
        config.report.include_non_software = True
    if getattr(args, "deep", False):
        # Opt in to the wide archive sweep for this run only; config.toml is
        # never rewritten behind the user's back.
        config.discovery.use_wayback = True


def _open_static(path: Path) -> int:
    """Open the optional static export, which is a capped snapshot."""

    if not path.exists():
        print(
            f"No static export at {path}. Run 'python -m jobfinder run' first.",
            file=sys.stderr,
        )
        return 1
    opened = webbrowser.open(path.resolve().as_uri())
    print(f"Opened {path}" if opened else f"Open this file in a browser: {path}")
    return 0


def command_serve(
    config: AppConfig,
    args: argparse.Namespace,
    *,
    open_browser: bool = False,
) -> int:
    """Serve the full-dataset dashboard from the local database.

    If something is already listening on the port, that is treated as an
    already-running dashboard: the URL is opened rather than starting a second
    copy and failing on the bind.
    """

    host = getattr(args, "host", server_module.DEFAULT_HOST)
    port = getattr(args, "port", server_module.DEFAULT_PORT)
    url = f"http://{host}:{port}"

    if server_module.is_running(host, port):
        print(f"Dashboard already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return 0

    if getattr(args, "refresh", False):
        database = Database(config.database_path)
        database.initialize()
        try:
            total = refresh_opportunities(config, database)
        finally:
            database.close()
        print(f"Rebuilt {total:,} canonical opportunities.")

    try:
        dashboard = server_module.serve(config, host=host, port=port)
    except OSError as exc:
        print(f"Could not start the dashboard on {url}: {exc}", file=sys.stderr)
        return 1

    dashboard.start()
    total = dashboard.summary().get("opportunities", 0)
    print(f"Dashboard: {dashboard.url}")
    print(f"  serving {total:,} canonical opportunities from {config.database_path}")
    print("  every filter, count and sort runs over the complete set")
    print("  press Ctrl+C to stop")
    if open_browser:
        webbrowser.open(dashboard.url)
    try:
        dashboard.serve_forever()
    finally:
        dashboard.stop()
        print("\nDashboard stopped.")
    return 0


def _explain_empty_registry(summary) -> None:
    """Say something useful when a run had no boards to contact at all.

    Without this, a first run where every discovery source was unreachable
    prints a summary of zeros and exits 0, which looks like "there are no jobs"
    rather than "nothing could be reached".
    """

    print(
        "\nNo Ashby boards are known yet, so nothing could be fetched.",
        file=sys.stderr,
    )
    failed = [
        entry for entry in summary.discovery_sources if entry.get("error")
    ]
    if failed:
        print("\nEvery discovery source failed:", file=sys.stderr)
        for entry in failed:
            print(f"  {entry['source']}: {entry['error']}", file=sys.stderr)
        print(
            "\nThis is usually a network problem — check your connection and "
            "run again. Discovery sources are independent, so a single source "
            "being down normally still leaves the others working.",
            file=sys.stderr,
        )
    elif not summary.discovery_sources:
        print(
            "\nNo discovery source ran. Check that discovery.enabled is true in "
            "config.toml and that at least one source is configured.",
            file=sys.stderr,
        )
    else:
        print(
            "\nDiscovery ran without errors but produced no usable board slugs. "
            "The configured sources may have changed format.",
            file=sys.stderr,
        )
    print(
        "\nYou can always add a board by hand, which needs no discovery at all:\n"
        "  python -m jobfinder add-company notion",
        file=sys.stderr,
    )


def command_run(config: AppConfig, args: argparse.Namespace) -> int:
    summary = run_pipeline(
        config,
        skip_discovery=args.skip_discovery,
        refresh_discovery=args.refresh_discovery,
        max_companies=args.max_companies,
        only=args.company,
        skip_reports=args.no_report,
    )
    print(format_summary(summary))
    if summary.boards_attempted and not summary.boards_succeeded:
        print(
            "\nEvery board fetch failed; existing jobs were left untouched. "
            "Check your network connection and try again.",
            file=sys.stderr,
        )
        return 1
    if not summary.companies_known:
        # A warning, not a failure. The reports were still written and the
        # dashboard still starts, so a transient network problem does not cost
        # the user their one command.
        _explain_empty_registry(summary)
    if summary.discovery_errors:
        # Boards were still fetched, so this is a warning, not a failure: one
        # provider going down must not look like the tool being broken.
        print(
            f"\nNote: {len(summary.discovery_errors)} discovery source issue(s) "
            "this run. Existing boards were refreshed normally.",
            file=sys.stderr,
        )
        for message in summary.discovery_errors:
            print(f"  {message}", file=sys.stderr)
    # The dashboard is the live server now, so a finished run hands off to it
    # rather than opening a capped snapshot.
    if getattr(args, "serve", False) or args.open:
        return command_serve(config, args, open_browser=args.open)
    return 0


def command_report(config: AppConfig) -> int:
    database = Database(config.database_path)
    database.initialize()
    try:
        report = write_reports(config, database)
        counts = database.summary_counts()
    finally:
        database.close()
    print(f"Dashboard: {report.dashboard_path} ({report.dashboard_jobs:,} jobs shown)")
    print(f"CSV:       {report.csv_path} ({report.csv_jobs:,} rows)")
    print(
        f"Database:  {config.database_path} "
        f"({counts['active_jobs']:,} active jobs, {counts['software_jobs']:,} software roles)"
    )
    return 0


def command_rescore(config: AppConfig) -> int:
    database = Database(config.database_path)
    database.initialize()
    try:
        updated = rescore_jobs(database, graduation_cohort=config.graduation_cohort)
        report = write_reports(config, database)
        counts = database.summary_counts()
    finally:
        database.close()
    print(f"Re-scored {updated:,} stored jobs with the current rules.")
    print(
        f"Software roles: {counts['software_jobs']:,} · "
        f"strong matches: {counts['strong_matches']:,}"
    )
    print(f"Dashboard: {report.dashboard_path} ({report.dashboard_jobs:,} jobs shown)")
    print(f"CSV:       {report.csv_path} ({report.csv_jobs:,} rows)")
    return 0


def command_discovery_report(config: AppConfig) -> int:
    """Print measured per-source discovery figures and their overlap."""

    database = Database(config.database_path)
    database.initialize()
    try:
        report = discovery_report(database)
    finally:
        database.close()

    sources = report["sources"]
    if not sources:
        print("No discovery provenance recorded yet. Run 'python -m jobfinder run' first.")
        return 0

    print("DISCOVERY SOURCES\n")
    header = f"{'source':<20}{'slugs':>9}{'only here':>11}{'registered':>12}{'verified':>10}{'active':>9}"
    print(header)
    print("-" * len(header))
    for row in sources:
        print(
            f"{str(row['discovery_source']):<20}"
            f"{int(row['unique_slugs']):>9,}"
            f"{int(row['exclusive_slugs']):>11,}"
            f"{int(row['registered']):>12,}"
            f"{int(row['verified']):>10,}"
            f"{int(row['active_boards']):>9,}"
        )
    print("-" * len(header))
    print(
        f"{'union':<20}{report['total_unique_slugs']:>9,}"
        f"{'':>11}{report['companies_registered']:>12,}"
        f"{report['boards_verified']:>10,}{report['boards_active']:>9,}"
    )
    if report["overlap_pairs"]:
        print("\nOVERLAP BETWEEN SOURCES\n")
        for pair in report["overlap_pairs"]:
            print(
                f"  {pair['left_source']} ∩ {pair['right_source']}: "
                f"{int(pair['shared']):,} shared slug(s)"
            )
    print("\nEvery slug above was verified against Ashby's own public API before")
    print("it was trusted; 'verified' counts the ones that returned a real board.")
    return 0


def command_stats(config: AppConfig) -> int:
    """Print geography and experience-evidence breakdowns for stored jobs."""

    database = Database(config.database_path)
    database.initialize()
    try:
        counts = database.summary_counts()
        geo = database.geo_tag_counts()
        experience = database.experience_category_counts()
    finally:
        database.close()

    considered = geo.pop("jobs_considered", 0)
    untagged = geo.pop("no_us_tag", 0)
    print("ACTIVE SOFTWARE ROLES\n")
    print(f"  {'total':<26}{considered:>10,}")
    print(f"  {'with a U.S. geography tag':<26}{considered - untagged:>10,}")
    print(f"  {'strong matches':<26}{counts['strong_matches']:>10,}")
    print("\nGEOGRAPHY (a job may carry several tags)\n")
    for tag in GEO_TAGS:
        print(f"  {geo_label(tag):<26}{geo.get(tag, 0):>10,}")
    print(f"  {'no U.S. tag':<26}{untagged:>10,}")
    print("\nEXPERIENCE EVIDENCE\n")
    for category, total in sorted(experience.items(), key=lambda item: -item[1]):
        print(f"  {experience_label(category):<26}{total:>10,}")
    print(
        "\n'Not stated' means the posting never mentioned experience. Those roles are"
        "\nkept in the dashboard on purpose — unknown is not the same as unsuitable."
    )
    return 0


def command_add(config: AppConfig, values: Sequence[str]) -> int:
    results = add_companies(config, values)
    failures = 0
    for result in results:
        if result["verified"]:
            print(
                f"verified  {result['slug']}  "
                f"{result.get('display_name') or ''} ({result.get('job_count', 0)} jobs)"
            )
        else:
            failures += 1
            print(
                f"rejected  {result['input']}: {result.get('error') or 'not a valid board'}",
                file=sys.stderr,
            )
    if any(result["verified"] for result in results):
        print("\nRun 'python -m jobfinder run' to ingest their jobs.")
    return 1 if failures and failures == len(results) else 0


def command_companies(config: AppConfig, args: argparse.Namespace) -> int:
    database = Database(config.database_path)
    database.initialize()
    try:
        companies = database.list_companies(source="ashby", active=None if args.all else True)
    finally:
        database.close()
    if not companies:
        print("No companies yet. Run 'python -m jobfinder run' to discover boards.")
        return 0
    print(f"{'SLUG':<28}{'JOBS':>6}  {'LAST SUCCESS':<22}{'STATUS'}")
    for company in companies[: max(0, args.limit)]:
        status = "active" if company["active"] else f"inactive({company['consecutive_failures']})"
        print(
            f"{company['slug'][:27]:<28}"
            f"{(company['last_job_count'] or 0):>6}  "
            f"{str(company['last_success_at'] or '-'):<22}{status}"
        )
    if len(companies) > args.limit:
        print(f"… {len(companies) - args.limit:,} more (use --limit)")
    return 0


def command_compliance(config: AppConfig, args: argparse.Namespace) -> int:
    """Run the technical compliance audit; non-zero only on a FAIL."""

    from .compliance import format_report, run_audit

    report = run_audit(config)
    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(format_report(report))
    return report.exit_code


def command_smoke_test(config: AppConfig, slugs: Sequence[str]) -> int:
    """Contact real boards and print what came back, without storing anything."""

    source = build_source(config)
    failures = 0
    try:
        for slug in slugs:
            try:
                board = source.fetch_company_jobs(slug)
            except (SourceError, ValueError) as exc:
                failures += 1
                print(f"FAIL  {slug}: {exc}", file=sys.stderr)
                continue
            software = 0
            best = None
            for job in board.jobs:
                result = analyze_normalized_job(job, attach=False)
                software += int(result.is_software_role)
                if best is None or result.score > best[0]:
                    best = (result.score, job.title, job.apply_url)
            print(
                f"OK    {slug}: {len(board.jobs)} jobs, {software} software roles "
                f"({board.board_url})"
            )
            if best:
                print(f"      best match {best[0]}/100 — {best[1]}")
                print(f"      apply: {best[2]}")
    finally:
        source.close()
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command is None:
        # Bare `python -m jobfinder` should do the useful thing.
        args = parser.parse_args([*arguments, "run"])
    command = args.command
    _configure_logging(args.verbose, args.quiet)

    try:
        config = load_config(args.config)
        _apply_overrides(config, args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        if command == "run":
            return command_run(config, args)
        if command == "serve":
            return command_serve(config, args, open_browser=getattr(args, "open", False))
        if command == "open":
            if getattr(args, "static", False):
                return _open_static(config.dashboard_path)
            return command_serve(config, args, open_browser=True)
        if command == "report":
            return command_report(config)
        if command == "rescore":
            return command_rescore(config)
        if command == "add-company":
            return command_add(config, args.slug)
        if command == "add-url":
            return command_add(config, args.url)
        if command == "companies":
            return command_companies(config, args)
        if command == "discovery-report":
            return command_discovery_report(config)
        if command == "stats":
            return command_stats(config)
        if command == "compliance":
            return command_compliance(config, args)
        if command == "smoke-test":
            return command_smoke_test(config, args.slug)
    except KeyboardInterrupt:
        print("\nInterrupted; the database was left consistent.", file=sys.stderr)
        return 130
    except Exception as exc:
        if args.verbose:
            raise
        print(f"Error: {exc}", file=sys.stderr)
        print("Re-run with --verbose for a full traceback.", file=sys.stderr)
        return 1

    parser.error(f"unknown command {command!r}")
    return 2


__all__ = ["build_parser", "main"]
