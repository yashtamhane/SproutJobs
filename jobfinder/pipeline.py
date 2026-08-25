"""End-to-end orchestration: discover, verify, ingest, analyze, report.

This module owns the order of operations and the failure policy.  Two rules
matter more than anything else here:

* A board is only reconciled (which is what closes vanished postings) after the
  source adapter returned a *complete* board.  Fetch failures touch the company
  registry and never the jobs.
* Nothing is written to the reports that did not come from a successful fetch.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .canonical import canonical_rows
from .config import AppConfig
from .db import Database
from .dedup import find_duplicate_groups
from .discovery.base import DiscoveryResult
from .discovery.registry import build_discoverers as _build_discoverers
from .discovery.registry import describe as describe_source
from .models import BoardFetchResult, NormalizedJob
from .normalize import ashby_board_url, extract_ashby_slug, normalize_ashby_job, utc_now
from .reporting import _normalize_job, generate_dashboard, write_csv
from .scoring import DEFAULT_GRADUATION_COHORT, analyze_normalized_job
from .sources.ashby import AshbySource
from .sources.base import BoardNotFoundError, SourceError
from .utils import parse_datetime, utc_now as _utc_now_datetime


LOGGER = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """Actual, measured results of one pipeline run."""

    run_id: int
    started_at: str
    completed_at: str = ""
    companies_known: int = 0
    companies_verified: int = 0
    companies_newly_discovered: int = 0
    active_jobs: int = 0
    software_jobs: int = 0
    likely_entry_level: int = 0
    strong_matches: int = 0
    new_strong_matches: int = 0
    boards_attempted: int = 0
    boards_succeeded: int = 0
    boards_failed: int = 0
    jobs_inserted: int = 0
    jobs_updated: int = 0
    jobs_reopened: int = 0
    jobs_unchanged: int = 0
    jobs_closed: int = 0
    discovery_candidates: int = 0
    discovery_unique_slugs: int = 0
    discovery_new_slugs: int = 0
    discovery_deferred: int = 0
    discovery_errors: tuple[str, ...] = ()
    discovery_sources: tuple[dict[str, Any], ...] = ()
    us_jobs: int = 0
    geo_counts: dict[str, int] = field(default_factory=dict)
    experience_counts: dict[str, int] = field(default_factory=dict)
    duplicate_groups: int = 0
    dashboard_jobs: int = 0
    csv_jobs: int = 0
    reportable_jobs: int = 0
    reportable_source_records: int = 0
    collapsed_duplicates: int = 0
    dashboard_path: str = ""
    csv_path: str = ""
    database_path: str = ""
    failures: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "companies_known": self.companies_known,
            "companies_verified": self.companies_verified,
            "companies_newly_discovered": self.companies_newly_discovered,
            "active_jobs": self.active_jobs,
            "software_jobs": self.software_jobs,
            "likely_entry_level": self.likely_entry_level,
            "strong_matches": self.strong_matches,
            "new_strong_matches": self.new_strong_matches,
            "boards_attempted": self.boards_attempted,
            "boards_succeeded": self.boards_succeeded,
            "boards_failed": self.boards_failed,
            "jobs_inserted": self.jobs_inserted,
            "jobs_updated": self.jobs_updated,
            "jobs_reopened": self.jobs_reopened,
            "jobs_unchanged": self.jobs_unchanged,
            "jobs_closed": self.jobs_closed,
            "discovery_candidates": self.discovery_candidates,
            "discovery_unique_slugs": self.discovery_unique_slugs,
            "discovery_new_slugs": self.discovery_new_slugs,
            "discovery_deferred": self.discovery_deferred,
            "discovery_errors": list(self.discovery_errors),
            "discovery_sources": list(self.discovery_sources),
            "us_jobs": self.us_jobs,
            "geo_counts": dict(self.geo_counts),
            "experience_counts": dict(self.experience_counts),
            "duplicate_groups": self.duplicate_groups,
            "dashboard_jobs": self.dashboard_jobs,
            "csv_jobs": self.csv_jobs,
            "reportable_jobs": self.reportable_jobs,
            "reportable_source_records": self.reportable_source_records,
            "collapsed_duplicates": self.collapsed_duplicates,
            "dashboard_path": self.dashboard_path,
            "csv_path": self.csv_path,
            "database_path": self.database_path,
        }


def build_source(config: AppConfig) -> AshbySource:
    """Create the Ashby adapter with the configured, respectful network limits."""

    return AshbySource(
        timeout=config.network.request_timeout,
        retries=config.network.retries,
        backoff_factor=config.network.backoff_seconds,
        min_request_interval=config.network.min_request_interval,
        include_compensation=config.search.include_compensation,
        user_agent=config.network.user_agent,
    )


def build_discoverers(config: AppConfig, session: Any | None = None) -> list[Any]:
    """Assemble the configured discovery plugins (empty when none are set)."""

    return list(_build_discoverers(config, session))


@dataclass
class SourceDiscoveryStats:
    """What one discovery plugin actually contributed this run."""

    source: str
    candidates: int = 0
    unique_slugs: int = 0
    new_slugs: int = 0
    from_cache: bool = False
    documents_read: int = 0
    documents_failed: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "description": describe_source(self.source),
            "candidates": self.candidates,
            "unique_slugs": self.unique_slugs,
            "new_slugs": self.new_slugs,
            "from_cache": self.from_cache,
            "documents_read": self.documents_read,
            "documents_failed": self.documents_failed,
            "error": self.error,
        }


@dataclass
class DiscoveryOutcome:
    """Aggregate discovery result, with the per-source detail preserved."""

    candidates_seen: int = 0
    unique_slugs: int = 0
    new_slugs: int = 0
    deferred: int = 0
    errors: tuple[str, ...] = ()
    per_source: tuple[SourceDiscoveryStats, ...] = ()

    def __iter__(self):
        """Stay compatible with ``seen, registered, errors = run_discovery(...)``."""

        return iter((self.candidates_seen, self.new_slugs, self.errors))


def run_discovery(
    config: AppConfig,
    database: Database,
    run_id: int,
    *,
    force: bool = False,
    discoverers: Sequence[Any] | None = None,
) -> DiscoveryOutcome:
    """Register newly seen candidate slugs from every enabled source.

    Candidates are never treated as companies here; they are recorded as
    unverified registry entries that the fetch stage must confirm against Ashby
    itself.  Which source produced which slug is written to
    ``discovery_slug_sources`` for *every* candidate, including ones already
    known, so per-source and overlap figures are measured rather than guessed.
    """

    providers = list(discoverers) if discoverers is not None else build_discoverers(config)
    outcome = DiscoveryOutcome()
    if not providers:
        return outcome

    known = {row["slug"].casefold() for row in database.list_companies(source="ashby")}
    limit = config.discovery.max_new_candidates_per_run
    errors: list[str] = []
    seen_this_run: set[str] = set()
    per_source: list[SourceDiscoveryStats] = []
    registered = 0
    deferred = 0

    for provider in providers:
        name = getattr(provider, "name", "discovery")
        stats = SourceDiscoveryStats(source=name)
        try:
            result: DiscoveryResult = provider.discover(force=force)
        except Exception as exc:  # a discovery plugin must never abort a run
            LOGGER.warning("Discovery provider %s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")
            stats.error = str(exc)
            per_source.append(stats)
            continue
        if result.error:
            errors.append(f"{name}: {result.error}")
            stats.error = result.error
        stats.from_cache = result.from_cache
        stats.documents_read = result.documents_read
        stats.documents_failed = result.documents_failed

        provenance: list[tuple[str, str, str | None]] = []
        source_slugs: set[str] = set()
        for candidate in result.candidates:
            stats.candidates += 1
            outcome.candidates_seen += 1
            key = candidate.slug.casefold()
            if key in source_slugs:
                continue
            source_slugs.add(key)
            provenance.append((candidate.slug, name, candidate.evidence))
            if key in seen_this_run:
                continue
            seen_this_run.add(key)
            if key in known:
                continue
            if registered >= limit:
                deferred += 1
                continue
            try:
                database.register_company_candidate(candidate, run_id=run_id)
            except ValueError as exc:
                LOGGER.debug("Rejected discovery candidate %r: %s", candidate.slug, exc)
                continue
            known.add(key)
            registered += 1
            stats.new_slugs += 1
        stats.unique_slugs = len(source_slugs)
        if provenance:
            database.record_slug_sources(provenance)
        per_source.append(stats)
        LOGGER.info(
            "Discovery source %s: %s unique slug(s), %s new%s",
            name,
            stats.unique_slugs,
            stats.new_slugs,
            " (from cache)" if result.from_cache else "",
        )

    if deferred:
        message = (
            f"discovery cap reached: registered {registered} new candidate(s) and deferred "
            f"{deferred} to a later run (discovery.max_new_candidates_per_run={limit})"
        )
        LOGGER.warning(message)
        errors.append(message)

    outcome.unique_slugs = len(seen_this_run)
    outcome.new_slugs = registered
    outcome.deferred = deferred
    outcome.errors = tuple(errors)
    outcome.per_source = tuple(per_source)
    return outcome


def discovery_report(database: Database) -> dict[str, Any]:
    """Measured per-source discovery figures straight from the database."""

    stats = database.discovery_source_stats()
    overlap = database.discovery_source_overlap()
    for row in stats:
        row["description"] = describe_source(str(row["discovery_source"]))
        row["exclusive_slugs"] = overlap["exclusive"].get(str(row["discovery_source"]), 0)
    companies = database.list_companies(source="ashby")
    return {
        "sources": stats,
        "overlap_pairs": overlap["pairs"],
        "total_unique_slugs": overlap["total_unique_slugs"],
        "companies_registered": len(companies),
        "boards_verified": sum(1 for row in companies if row.get("last_success_at")),
        "boards_active": sum(1 for row in companies if row.get("active")),
    }


def select_companies(
    config: AppConfig,
    database: Database,
    *,
    now: datetime | None = None,
    max_companies: int | None = None,
    only: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Choose which registry entries to contact this run.

    Active boards are always refreshed.  Never-verified candidates get their
    first chance, and boards deactivated by repeated failures are retried only
    after the configured cool-off so a dead slug is not hammered every run.
    """

    if only:
        wanted = {slug.casefold() for slug in only}
        return [
            company
            for company in database.list_companies(source="ashby")
            if company["slug"].casefold() in wanted
        ]

    reference = now or _utc_now_datetime()
    retry_after = timedelta(hours=max(0, config.discovery.retry_inactive_after_hours))
    selected: list[dict[str, Any]] = []
    for company in database.list_companies(source="ashby"):
        if company["active"]:
            selected.append(company)
            continue
        last_verified = parse_datetime(company.get("last_verified_at"))
        if last_verified is None or reference - last_verified >= retry_after:
            selected.append(company)

    # Fresh candidates first so a newly discovered board shows up the same run,
    # then boards that have gone longest without a successful fetch.
    def order(company: dict[str, Any]) -> tuple[int, str]:
        never_fetched = company.get("last_success_at") is None
        return (0 if never_fetched else 1, str(company.get("last_success_at") or ""))

    selected.sort(key=order)
    if max_companies is not None:
        selected = selected[:max_companies]
    return selected


def _ingest_board(
    database: Database,
    board: BoardFetchResult,
    run_id: int,
    graduation_cohort: tuple[int, int] = DEFAULT_GRADUATION_COHORT,
) -> dict[str, int]:
    """Analyze then persist one complete board inside a single transaction."""

    for job in board.jobs:
        analyze_normalized_job(job, graduation_cohort=graduation_cohort)
    stats = database.ingest_company_jobs(
        board.slug,
        board.jobs,
        run_id,
        source=board.source,
        display_name=board.display_name,
        board_url=board.board_url,
        fetched_at=board.fetched_at,
    )
    return stats.as_dict()


def fetch_and_ingest(
    config: AppConfig,
    database: Database,
    run_id: int,
    companies: Sequence[dict[str, Any]],
    *,
    source: Any | None = None,
    summary: RunSummary | None = None,
) -> RunSummary:
    """Fetch every selected board concurrently and ingest successful results.

    Network work happens in a small worker pool; every database write happens on
    this thread as results arrive, which keeps SQLite access single-threaded and
    each board's reconciliation atomic.
    """

    result = summary or RunSummary(run_id=run_id, started_at=utc_now())
    if not companies:
        return result

    adapter = source or build_source(config)
    owns_adapter = source is None
    failures: list[tuple[str, str]] = list(result.failures)
    workers = max(1, min(config.network.concurrency, len(companies)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(adapter.fetch_company_jobs, company["slug"]): company
                for company in companies
            }
            for future in as_completed(futures):
                company = futures[future]
                slug = company["slug"]
                result.boards_attempted += 1
                try:
                    board = future.result()
                except BoardNotFoundError as exc:
                    LOGGER.info("Board %s does not exist: %s", slug, exc)
                    failures.append((slug, str(exc)))
                    result.boards_failed += 1
                    database.record_company_failure(
                        slug,
                        str(exc),
                        inactive_after=config.database.inactive_after_failures,
                    )
                    continue
                except (SourceError, ValueError) as exc:
                    LOGGER.warning("Board %s failed: %s", slug, exc)
                    failures.append((slug, str(exc)))
                    result.boards_failed += 1
                    database.record_company_failure(
                        slug,
                        str(exc),
                        inactive_after=config.database.inactive_after_failures,
                    )
                    continue
                except Exception as exc:  # unexpected, but one board must not abort the run
                    LOGGER.exception("Unexpected failure fetching board %s", slug)
                    failures.append((slug, f"{type(exc).__name__}: {exc}"))
                    result.boards_failed += 1
                    database.record_company_failure(
                        slug,
                        f"{type(exc).__name__}: {exc}",
                        inactive_after=config.database.inactive_after_failures,
                    )
                    continue

                try:
                    stats = _ingest_board(
                        database, board, run_id, config.graduation_cohort
                    )
                except Exception as exc:
                    # Ingestion is transactional, so nothing partial was stored.
                    LOGGER.exception("Could not persist board %s", slug)
                    failures.append((slug, f"ingest failed: {exc}"))
                    result.boards_failed += 1
                    continue

                result.boards_succeeded += 1
                result.jobs_inserted += stats["inserted"]
                result.jobs_updated += stats["updated"]
                result.jobs_reopened += stats["reopened"]
                result.jobs_unchanged += stats["unchanged"]
                result.jobs_closed += stats["closed"]
                LOGGER.debug(
                    "%s: %s seen, %s new, %s updated, %s closed",
                    slug,
                    stats["seen"],
                    stats["inserted"],
                    stats["updated"],
                    stats["closed"],
                )
    finally:
        if owns_adapter:
            adapter.close()

    result.failures = tuple(failures)
    return result


def selected_records(config: AppConfig, database: Database) -> list[dict[str, Any]]:
    """Select the persisted source records the reports are allowed to describe."""

    rows = database.list_jobs(active=None)
    cutoff = ""
    if config.report.include_closed_days:
        cutoff = (
            _utc_now_datetime() - timedelta(days=config.report.include_closed_days)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

    selected: list[dict[str, Any]] = []
    for row in rows:
        if not row["active"]:
            if not config.report.include_closed_days:
                continue
            last_seen = str(row.get("last_seen_at") or "")
            if cutoff and last_seen < cutoff:
                continue
        if not config.report.include_non_software and not row.get("is_software"):
            continue
        # Geography is U.S.-wide.  A posting is dropped only when it is
        # *explicitly* outside the United States, never for being outside
        # NYC/NJ and never for having geography that is merely unclear.
        if not config.report.include_non_us and _explicitly_non_us(row):
            continue
        score = row.get("search_score")
        if score is not None and score < config.search.min_score:
            continue
        selected.append(row)
    return selected


def _explicitly_non_us(row: dict[str, Any]) -> bool:
    """True only when the posting states a location and none of it is U.S."""

    if row.get("geo_tags"):
        return False
    flags = row.get("eligibility_flags") or ()
    return "outside_us" in flags or "outside_united_states" in flags


def report_rows(config: AppConfig, database: Database) -> list[dict[str, Any]]:
    """Return the distinct opportunities both reports describe.

    Source records stay untouched in SQLite; this collapses the ones that
    describe the same opening so a reader sees each opportunity once.  Both
    reports are built from this list, which is what keeps them in agreement.
    """

    selected = selected_records(config, database)
    opportunities = canonical_rows(selected)
    collapsed = len(selected) - len(opportunities)
    if collapsed:
        LOGGER.info(
            "Canonicalized %s source records into %s distinct opportunities (%s collapsed)",
            len(selected),
            len(opportunities),
            collapsed,
        )
    return opportunities


def opportunity_rows_from(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn canonical opportunity records into dashboard card rows.

    This uses the same normalizer the static report and the CSV use, so every
    surface describes identical opportunities.  A ``search_blob`` is added so
    the server can search in SQL without ever loading a description.
    """

    prepared: list[dict[str, Any]] = []
    for row, source in zip((_normalize_job(item) for item in rows), rows):
        row["job_id"] = int(source.get("id") or 0)
        row["canonical_key"] = source.get("canonical_key")
        row["search_blob"] = " ".join(
            str(part)
            for part in (
                row["company"],
                row["title"],
                row["department"],
                row["location"],
                row["secondary_locations"],
                row["workplace_type"],
                row["employment_type"],
                row["compensation"],
                row["authorization_flag"],
                row["experience_label"],
                row["experience_summary"],
                *row["geo_labels"],
                *(
                    piece
                    for reason in row["score_reasons"]
                    for piece in (reason.get("label"), reason.get("evidence"))
                    if piece
                ),
                *row["warning_flags"],
            )
            if part
        ).casefold()
        prepared.append(row)
    return prepared


def opportunity_rows(config: AppConfig, database: Database) -> list[dict[str, Any]]:
    """Build dashboard card rows straight from the database."""

    return opportunity_rows_from(report_rows(config, database))


def refresh_opportunities(config: AppConfig, database: Database) -> int:
    """Rebuild the materialized opportunity table the dashboard serves.

    Called after every run and on demand, so the dashboard never serves a set
    that has drifted from what is stored.
    """

    return database.replace_opportunities(opportunity_rows(config, database))


def _dashboard_priority(row: dict[str, Any]) -> tuple[Any, ...]:
    """Rank rows so a capped dashboard keeps the most useful ones."""

    return (
        not row.get("active"),
        not row.get("is_software"),
        -(row.get("search_score") or 0),
        # Newest first within a score band; string dates are ISO-8601 and sort.
        _descending_text(str(row.get("published_at") or row.get("first_seen_at") or "")),
    )


def _descending_text(value: str) -> tuple[int, str]:
    """Sort non-empty ISO timestamps newest-first ahead of missing ones."""

    return (0 if value else 1, "".join(chr(0x10FFFD - ord(char)) for char in value))


@dataclass(frozen=True)
class ReportPaths:
    dashboard_path: Path
    csv_path: Path
    dashboard_jobs: int
    csv_jobs: int
    source_records: int = 0

    @property
    def collapsed_duplicates(self) -> int:
        return max(0, self.source_records - self.csv_jobs)


def write_reports(config: AppConfig, database: Database) -> ReportPaths:
    """Regenerate both reports from persisted data only.

    The CSV is the complete export.  The dashboard renders one card per row, so
    it is capped by ``report.max_dashboard_jobs`` and the cap is always reported
    rather than applied silently.
    """

    selected = selected_records(config, database)
    rows = canonical_rows(selected)
    if len(selected) != len(rows):
        LOGGER.info(
            "Canonicalized %s source records into %s distinct opportunities (%s collapsed)",
            len(selected),
            len(rows),
            len(selected) - len(rows),
        )
    dashboard_rows = sorted(rows, key=_dashboard_priority)
    limit = config.report.max_dashboard_jobs
    if len(dashboard_rows) > limit:
        LOGGER.info(
            "Dashboard limited to the top %s of %s matching jobs (report.max_dashboard_jobs)",
            limit,
            len(dashboard_rows),
        )
        dashboard_rows = dashboard_rows[:limit]

    counts = database.summary_counts()
    config.output_directory.mkdir(parents=True, exist_ok=True)
    dashboard_path = generate_dashboard(
        dashboard_rows,
        config.dashboard_path,
        title="SproutJobs",
        totals={
            "jobs_tracked": counts["active_jobs"],
            "active_jobs": counts["active_jobs"],
            "software_jobs": counts["software_jobs"],
            "strong_matches": counts["strong_matches"],
            "new_jobs": sum(1 for row in rows if row.get("new_since_previous_run")),
            "reportable_jobs": len(rows),
        },
        default_freshness_days=config.search.days_to_show,
    )
    csv_file = write_csv(rows, config.csv_path)
    # Materialize the same canonical set for the local dashboard, so its
    # filters and counts cover every opportunity rather than the capped page.
    database.replace_opportunities(opportunity_rows_from(rows))
    return ReportPaths(
        dashboard_path, csv_file, len(dashboard_rows), len(rows), len(selected)
    )


def rescore_jobs(
    database: Database,
    *,
    batch_size: int = 500,
    graduation_cohort: tuple[int, int] = DEFAULT_GRADUATION_COHORT,
) -> int:
    """Re-run classification and scoring over every stored posting.

    The heuristics evolve; stored jobs should not keep a verdict from an older
    ruleset.  Source data and history are untouched — only derived analysis is
    rewritten, and it is recomputed from each posting's own stored snapshot.

    This is also how a changed ``candidate.graduation_date`` reaches postings
    that were ingested under the previous one.
    """

    updates: list[dict[str, Any]] = []
    written = 0
    for row in database.list_jobs(active=None):
        job = _job_from_row(row)
        analyze_normalized_job(job, graduation_cohort=graduation_cohort)
        updates.append(
            {
                "job_id": row["id"],
                "is_software": job.is_software,
                "search_score": job.search_score,
                "eligibility_category": job.match_category,
                "eligibility_flags": job.eligibility_flags,
                "score_reasons": job.score_reasons,
                "authorization_flag": job.authorization_flag,
                "uncertain": job.uncertain,
                "semantic_duplicate_key": job.semantic_duplicate_key,
                "evidence_confidence": job.evidence_confidence,
                "entry_level_score": job.entry_level_score,
                "experience_category": job.experience_category,
                "experience_summary": job.experience_summary,
                "experience_detail": job.experience_detail,
                "blocker_status": job.blocker_status,
                "geo_tags": job.geo_tags,
            }
        )
        if len(updates) >= batch_size:
            written += database.update_job_analyses(updates)
            updates.clear()
    written += database.update_job_analyses(updates)
    return written


def _job_from_row(row: dict[str, Any]) -> NormalizedJob:
    """Rebuild a normalized job from persisted data for re-analysis."""

    raw = row.get("raw_data")
    if isinstance(raw, dict) and raw:
        # Descriptions live in their own columns rather than being duplicated in
        # the source snapshot, so put them back before re-normalizing.
        restored = dict(raw)
        if row.get("description_html"):
            restored.setdefault("descriptionHtml", row["description_html"])
        if row.get("description_text"):
            restored.setdefault("descriptionPlain", row["description_text"])
        try:
            return normalize_ashby_job(
                restored, row["source_company_slug"], row.get("company_name")
            )
        except (TypeError, ValueError):
            LOGGER.debug("Falling back to stored columns for job %s", row.get("id"))
    return NormalizedJob(
        source=row.get("source", "ashby"),
        source_company_slug=row.get("source_company_slug", ""),
        source_job_id=row.get("source_job_id", ""),
        company_name=row.get("company_name", ""),
        title=row.get("title", ""),
        department=row.get("department"),
        team=row.get("team"),
        location=row.get("location"),
        secondary_locations=tuple(row.get("secondary_locations") or ()),
        workplace_type=row.get("workplace_type"),
        employment_type=row.get("employment_type"),
        description_text=row.get("description_text") or "",
    )


def count_duplicate_groups(database: Database) -> int:
    """Flag (never merge) active postings that look like the same opening."""

    active = database.list_jobs(active=True)
    return len(find_duplicate_groups(active))


def run_pipeline(
    config: AppConfig,
    *,
    skip_discovery: bool = False,
    refresh_discovery: bool = False,
    max_companies: int | None = None,
    only: Sequence[str] | None = None,
    skip_reports: bool = False,
    source: Any | None = None,
    discoverers: Sequence[Any] | None = None,
    database: Database | None = None,
) -> RunSummary:
    """Run every stage and return the measured summary."""

    owns_database = database is None
    store = database or Database(config.database_path)
    store.initialize()
    started = utc_now()
    run_id = store.start_run(
        "run",
        {
            "skip_discovery": skip_discovery,
            "refresh_discovery": refresh_discovery,
            "max_companies": max_companies,
            "config_path": str(config.config_path) if config.config_path else None,
        },
        started_at=started,
    )
    summary = RunSummary(run_id=run_id, started_at=started)
    summary.database_path = str(config.database_path)

    try:
        if not skip_discovery and config.discovery.enabled and not only:
            outcome = run_discovery(
                config,
                store,
                run_id,
                force=refresh_discovery,
                discoverers=discoverers,
            )
            summary.discovery_candidates = outcome.candidates_seen
            summary.discovery_unique_slugs = outcome.unique_slugs
            summary.discovery_new_slugs = outcome.new_slugs
            summary.discovery_deferred = outcome.deferred
            summary.discovery_errors = outcome.errors
            summary.discovery_sources = tuple(
                item.as_dict() for item in outcome.per_source
            )
            for message in outcome.errors:
                LOGGER.warning("Discovery issue: %s", message)

        companies = select_companies(
            config, store, max_companies=max_companies, only=only
        )
        LOGGER.info("Contacting %s Ashby board(s)", len(companies))
        fetch_and_ingest(config, store, run_id, companies, source=source, summary=summary)

        summary.duplicate_groups = count_duplicate_groups(store)
        counts = store.summary_counts(run_id)
        summary.companies_known = counts["companies_known"]
        summary.companies_verified = counts["companies_verified"]
        summary.companies_newly_discovered = counts["companies_newly_discovered"]
        summary.active_jobs = counts["active_jobs"]
        summary.software_jobs = counts["software_jobs"]
        summary.likely_entry_level = counts["likely_entry_level"]
        summary.strong_matches = counts["strong_matches"]
        summary.new_strong_matches = counts["new_strong_matches"]
        summary.geo_counts = store.geo_tag_counts()
        summary.experience_counts = store.experience_category_counts()
        summary.us_jobs = summary.geo_counts.get("jobs_considered", 0) - summary.geo_counts.get(
            "no_us_tag", 0
        )

        if not skip_reports:
            report = write_reports(config, store)
            summary.dashboard_path = str(report.dashboard_path)
            summary.csv_path = str(report.csv_path)
            summary.dashboard_jobs = report.dashboard_jobs
            summary.csv_jobs = report.csv_jobs
            summary.reportable_jobs = report.csv_jobs
            summary.reportable_source_records = report.source_records
            summary.collapsed_duplicates = report.collapsed_duplicates

        summary.completed_at = utc_now()
        status = "success" if not summary.boards_failed else "partial_success"
        store.finish_run(run_id, status, summary.as_dict())
    except BaseException as exc:
        summary.completed_at = utc_now()
        try:
            store.finish_run(run_id, "failed", summary.as_dict(), error=str(exc))
        except Exception:  # pragma: no cover - the original error matters more
            LOGGER.exception("Could not record run failure")
        raise
    finally:
        if owns_database:
            store.close()
    return summary


def add_companies(
    config: AppConfig,
    values: Iterable[str],
    *,
    source: Any | None = None,
    database: Database | None = None,
) -> list[dict[str, Any]]:
    """Verify and register user-provided slugs or Ashby board URLs.

    A seed is still only trusted after Ashby's own endpoint confirms it, exactly
    like an automatically discovered candidate.
    """

    owns_database = database is None
    store = database or Database(config.database_path)
    store.initialize()
    adapter = source or build_source(config)
    owns_adapter = source is None
    results: list[dict[str, Any]] = []
    try:
        for value in values:
            slug = extract_ashby_slug(value)
            if slug is None:
                results.append(
                    {"input": value, "slug": None, "verified": False, "error": "not an Ashby slug or board URL"}
                )
                continue
            validation = adapter.validate_company(slug)
            store.record_discovery_event(
                slug,
                "user_seed",
                discovery_evidence=value,
                candidate_url=ashby_board_url(slug),
                status="verified" if validation.valid else "rejected",
                message=validation.error,
            )
            if validation.valid:
                store.record_company_success(
                    slug,
                    display_name=validation.display_name,
                    board_url=validation.board_url,
                    job_count=validation.job_count,
                )
            else:
                store.record_company_failure(
                    slug,
                    validation.error or "validation failed",
                    inactive_after=config.database.inactive_after_failures,
                )
            results.append(
                {
                    "input": value,
                    "slug": slug,
                    "verified": validation.valid,
                    "display_name": validation.display_name,
                    "job_count": validation.job_count,
                    "board_url": validation.board_url,
                    "error": validation.error,
                }
            )
    finally:
        if owns_adapter:
            adapter.close()
        if owns_database:
            store.close()
    return results


def format_summary(summary: RunSummary) -> str:
    """Render the end-of-run report using only measured values."""

    def line(label: str, value: int) -> str:
        return f"{label:<28}{value:>10,}"

    parts = [
        "SPROUTJOBS",
        "",
        line("Companies known:", summary.companies_known),
        line("Companies verified:", summary.companies_verified),
        line("Companies newly discovered:", summary.companies_newly_discovered),
        "",
        line("Active jobs:", summary.active_jobs),
        line("Software jobs:", summary.software_jobs),
        line("U.S. software jobs:", summary.us_jobs),
        line("Likely entry-level:", summary.likely_entry_level),
        line("Strong matches:", summary.strong_matches),
        line("New strong matches:", summary.new_strong_matches),
        "",]
    if summary.geo_counts:
        from .geo import GEO_TAGS, label_for

        parts.append("Geography (a job may carry several tags):")
        parts.extend(
            f"  {label_for(tag):<24}{summary.geo_counts.get(tag, 0):>10,}" for tag in GEO_TAGS
        )
        parts.append("")
    if summary.experience_counts:
        from .experience import label_for as experience_label

        parts.append("Experience evidence:")
        for category, total in sorted(
            summary.experience_counts.items(), key=lambda item: -item[1]
        ):
            parts.append(f"  {experience_label(category):<24}{total:>10,}")
        parts.append("")
    parts.extend([
        line("Boards fetched:", summary.boards_succeeded),
        line("Boards failed:", summary.boards_failed),
        line("Jobs added this run:", summary.jobs_inserted),
        line("Jobs updated this run:", summary.jobs_updated),
        line("Jobs closed this run:", summary.jobs_closed),
    ])
    if summary.discovery_sources:
        parts.append("")
        parts.append("Discovery sources this run:")
        for entry in summary.discovery_sources:
            note = " (cached)" if entry.get("from_cache") else ""
            if entry.get("error"):
                note = " (failed)"
            parts.append(
                f"  {str(entry['source']):<20}"
                f"{int(entry.get('unique_slugs', 0)):>8,} slugs"
                f"{int(entry.get('new_slugs', 0)):>8,} new{note}"
            )
        if summary.discovery_deferred:
            parts.append(
                f"  {summary.discovery_deferred:,} candidate(s) deferred to the next run"
            )
    if summary.duplicate_groups:
        parts.append(line("Duplicate groups flagged:", summary.duplicate_groups))
    if summary.collapsed_duplicates:
        parts.append(line("Duplicate postings merged:", summary.collapsed_duplicates))
    if summary.dashboard_path:
        parts.extend(
            [
                "",
                "Dashboard:",
                f"  python -m jobfinder open   (serves all {summary.csv_jobs:,} opportunities)",
                "",
                "CSV:",
                f"  {summary.csv_path}",
                f"  {summary.csv_jobs:,} rows",
                "",
                "Static export (optional, capped):",
                f"  {summary.dashboard_path}",
                f"  {summary.dashboard_jobs:,} of {summary.csv_jobs:,} cards embedded",
            ]
        )
    parts.extend(["", "Database:", f"  {summary.database_path}"])
    if summary.failures:
        shown = summary.failures[:5]
        parts.append("")
        parts.append(f"{len(summary.failures)} board(s) failed this run; jobs were left untouched:")
        parts.extend(f"  {slug}: {error}" for slug, error in shown)
        if len(summary.failures) > len(shown):
            parts.append(f"  … and {len(summary.failures) - len(shown)} more (use --verbose)")
    return "\n".join(parts)


__all__ = [
    "DiscoveryOutcome",
    "opportunity_rows",
    "opportunity_rows_from",
    "refresh_opportunities",
    "RunSummary",
    "SourceDiscoveryStats",
    "add_companies",
    "build_discoverers",
    "build_source",
    "count_duplicate_groups",
    "discovery_report",
    "fetch_and_ingest",
    "format_summary",
    "report_rows",
    "rescore_jobs",
    "run_discovery",
    "selected_records",
    "run_pipeline",
    "select_companies",
    "write_reports",
]
