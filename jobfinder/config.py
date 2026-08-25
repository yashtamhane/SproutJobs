"""Configuration loading with conservative, candidate-focused defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, TypeVar


# Public, openly published job-list documents that already contain Ashby board
# links.  Each was checked to actually yield candidates; every slug found is
# still verified against Ashby's own endpoint before it is trusted.
DEFAULT_DISCOVERY_URLS = [
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
    "https://raw.githubusercontent.com/vanshb03/New-Grad-2026/main/README.md",
    "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/speedyapply/2026-SWE-College-Jobs/main/NEW_GRAD_USA.md",
    "https://raw.githubusercontent.com/speedyapply/2026-SWE-College-Jobs/main/README.md",
    "https://raw.githubusercontent.com/pittcsc/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/Ouckah/Summer2025-Internships/main/README.md",
    "https://raw.githubusercontent.com/coderQuad/New-Grad-Positions/master/README.md",
    "https://raw.githubusercontent.com/ReaVNaiL/New-Grad-2024/main/README.md",
    "https://raw.githubusercontent.com/jobright-ai/2025-Software-Engineer-New-Grad/master/README.md",
    "https://raw.githubusercontent.com/cvrve/New-Grad-2025/main/README.md",
]


@dataclass
class CandidateConfig:
    """The two settings that describe who is searching.

    Everything else about *where* and *what* is filtered in the dashboard, over
    the complete dataset, so it does not need to be decided ahead of time.
    """

    #: Collection scope.  SproutJobs targets United States roles; postings whose
    #: only location is elsewhere are still stored but left out of the reports
    #: unless ``report.include_non_us`` is turned on.
    country: str = "US"
    #: The graduation cohort the eligibility rules compare a posting against.
    #: A posting that says "Class of 2026" is compatible with this date and one
    #: that says "must graduate in 2028" is not.  Postings that say nothing
    #: about graduation — the large majority — are unaffected either way.
    graduation_date: str = "2026-05-31"


@dataclass
class SearchConfig:
    min_score: int = 0
    max_required_years: int = 2
    # Pre-selects the dashboard's freshness filter.  0 means "any time", which
    # is the safe default: plenty of still-open postings are months old, and
    # hiding them by default would bury good matches.
    days_to_show: int = 0
    include_possible_matches: bool = True
    include_compensation: bool = True


#: Identifies the client without claiming to be a browser.  Users are
#: encouraged to append their own contact URL; see config.toml.
DEFAULT_USER_AGENT = "SproutJobs/1.0 (local job-search client; respectful public API use)"


@dataclass
class NetworkConfig:
    request_timeout: float = 20.0
    #: Bounded on purpose.  ``_validate`` refuses anything above 8, so no
    #: configuration can turn this into a wide fan-out against someone's site.
    concurrency: int = 4
    retries: int = 3
    backoff_seconds: float = 0.75
    min_request_interval: float = 0.15
    user_agent: str = DEFAULT_USER_AGENT


@dataclass
class DiscoveryConfig:
    """Which free public sources are consulted for candidate board slugs.

    Nothing here requires an account, an API key, or a paid plan.  Every source
    is either an openly published document or an index a public archive offers
    for programmatic use.  Slug guessing is not a source and never will be.

    Sources are audited individually in SOURCES.md, and which of them are on by
    default is a deliberate decision recorded there, not an accident of what
    happened to be reachable.
    """

    enabled: bool = True
    refresh_hours: int = 24
    # Archive indexes change slowly and are large, so they are refetched weekly
    # rather than daily.
    archive_refresh_hours: int = 168
    # Archive discovery routinely finds thousands of candidates.  The cap exists
    # to bound one run's verification work, not to throttle discovery itself;
    # whatever is deferred is registered on the next run.
    max_new_candidates_per_run: int = 6000
    retry_inactive_after_hours: int = 168
    public_dataset_urls: List[str] = field(default_factory=lambda: list(DEFAULT_DISCOVERY_URLS))
    public_page_urls: List[str] = field(default_factory=list)
    # -- Free public web-archive indexes -----------------------------------
    #
    # Common Crawl: OFF, and it should stay off.  index.commoncrawl.org serves
    # ``Disallow: /`` with a short allow-list that does not include the
    # ``CC-MAIN-*-index`` query endpoint this would need.  Their stated policy
    # is honored rather than argued with.  ``jobfinder compliance`` reports
    # turning this on as a FAIL.  See SOURCES.md.
    use_common_crawl: bool = False
    common_crawl_indexes: int = 2
    # Internet Archive Wayback CDX: OFF by default, and safe to turn on.
    #
    # This is not a policy question — web.archive.org publishes no robots.txt
    # and the CDX index is documented for programmatic use.  It is off because
    # of what it costs *other people*: it widens discovery from roughly 600
    # candidate boards to roughly 5,000, which turns a two-minute first run into
    # a long one and asks Ashby for thousands of boards on every user's machine.
    # Set ``use_wayback = true`` (or run ``./run.sh --deep``) when you want the
    # full sweep and have read what it implies.  See SOURCES.md.
    use_wayback: bool = False
    wayback_limit: int = 200000
    # Opt-in only, and empty by default: see jobfinder/discovery/search.py.
    search_endpoints: List[str] = field(default_factory=list)
    # A local file of slugs or board URLs, one per line.
    seed_file: str = "seeds.txt"
    respect_robots: bool = True


@dataclass
class DatabaseConfig:
    path: str = "data/jobfinder.sqlite3"
    inactive_after_failures: int = 3


@dataclass
class OutputConfig:
    directory: str = "output"
    dashboard: str = "index.html"
    csv: str = "jobs.csv"


@dataclass
class ReportConfig:
    """Controls how much of the tracked history reaches the two reports."""

    # A full registry can track tens of thousands of postings.  The dashboard
    # renders every row it is given, so it is capped; the CSV is not.
    max_dashboard_jobs: int = 1500
    include_non_software: bool = False
    include_closed_days: int = 14
    # Collection is U.S.-wide.  Postings whose only location is outside the
    # United States are still stored and still visible in the database; they are
    # left out of the two reports because the candidate cannot take them.  A
    # posting whose geography is merely *unclear* is always kept.
    include_non_us: bool = False


@dataclass
class AppConfig:
    candidate: CandidateConfig = field(default_factory=CandidateConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    config_path: Optional[Path] = field(default=None, repr=False)
    project_root: Path = field(default_factory=Path.cwd, repr=False)

    @property
    def database_path(self) -> Path:
        return _resolve(self.project_root, self.database.path)

    @property
    def output_directory(self) -> Path:
        return _resolve(self.project_root, self.output.directory)

    @property
    def dashboard_path(self) -> Path:
        return self.output_directory / self.output.dashboard

    @property
    def csv_path(self) -> Path:
        return self.output_directory / self.output.csv

    @property
    def discovery_cache_directory(self) -> Path:
        return self.database_path.parent / "discovery-cache"

    @property
    def graduation_cohort(self) -> tuple[int, int]:
        """``candidate.graduation_date`` as ``(year, month)`` for the rules.

        An unparseable or empty value falls back to the rule defaults rather
        than raising, so a malformed date degrades scoring gracefully instead of
        stopping a run.  ``_validate`` reports the malformed value separately.
        """

        return _parse_cohort(self.candidate.graduation_date)


T = TypeVar("T")


#: What the eligibility rules assume when no graduation date is configured.
DEFAULT_GRADUATION_COHORT = (2026, 5)


def _parse_cohort(value: str) -> tuple[int, int]:
    """Read ``YYYY-MM-DD`` (or ``YYYY-MM``) into ``(year, month)``."""

    parts = str(value or "").strip().split("-")
    try:
        year, month = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return DEFAULT_GRADUATION_COHORT
    if not (1900 <= year <= 2200 and 1 <= month <= 12):
        return DEFAULT_GRADUATION_COHORT
    return year, month


def _resolve(root: Path, value: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else root / path


def _merge_dataclass(instance: T, values: Mapping[str, Any]) -> T:
    """Apply known TOML keys while rejecting typos early."""

    known = {item.name: item for item in fields(instance)}  # type: ignore[arg-type]
    unknown = set(values) - set(known)
    if unknown:
        typename = type(instance).__name__
        raise ValueError("Unknown {} setting(s): {}".format(typename, ", ".join(sorted(unknown))))
    for key, value in values.items():
        current = getattr(instance, key)
        if is_dataclass(current):
            if not isinstance(value, Mapping):
                raise ValueError("Configuration section {!r} must be a table".format(key))
            _merge_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def _validate(config: AppConfig) -> None:
    if not 0 <= config.search.min_score <= 100:
        raise ValueError("search.min_score must be between 0 and 100")
    if config.search.days_to_show < 0:
        raise ValueError("search.days_to_show cannot be negative")
    if config.network.request_timeout <= 0:
        raise ValueError("network.request_timeout must be positive")
    if not 1 <= config.network.concurrency <= 16:
        raise ValueError("network.concurrency must be between 1 and 16")
    if not 0 <= config.network.retries <= 8:
        raise ValueError("network.retries must be between 0 and 8")
    if config.network.min_request_interval < 0:
        raise ValueError("network.min_request_interval cannot be negative")
    if config.discovery.max_new_candidates_per_run < 0:
        raise ValueError("discovery.max_new_candidates_per_run cannot be negative")
    if config.discovery.common_crawl_indexes < 1:
        raise ValueError("discovery.common_crawl_indexes must be at least 1")
    if config.discovery.wayback_limit < 1000:
        raise ValueError("discovery.wayback_limit must be at least 1000")
    if config.discovery.archive_refresh_hours < 0:
        raise ValueError("discovery.archive_refresh_hours cannot be negative")
    if config.database.inactive_after_failures < 1:
        raise ValueError("database.inactive_after_failures must be at least 1")
    if config.report.max_dashboard_jobs < 1:
        raise ValueError("report.max_dashboard_jobs must be at least 1")
    if config.report.include_closed_days < 0:
        raise ValueError("report.include_closed_days cannot be negative")
    if not config.output.dashboard.strip() or not config.output.csv.strip():
        raise ValueError("output.dashboard and output.csv must be filenames")
    graduation = config.candidate.graduation_date
    if graduation and _parse_cohort(graduation) == DEFAULT_GRADUATION_COHORT:
        year, month = DEFAULT_GRADUATION_COHORT
        if not graduation.strip().startswith(f"{year:04d}-{month:02d}"):
            raise ValueError(
                "candidate.graduation_date must look like YYYY-MM-DD, "
                f"not {graduation!r}"
            )


def load_config(path: Optional[os.PathLike] = None) -> AppConfig:
    """Load ``config.toml`` or return defaults when the file is absent."""

    requested = Path(path) if path is not None else Path("config.toml")
    config = AppConfig()
    config.project_root = requested.resolve().parent
    config.config_path = requested.resolve() if requested.exists() else None
    if requested.exists():
        with requested.open("rb") as handle:
            raw: Dict[str, Any] = tomllib.load(handle)
        _merge_dataclass(config, raw)
        config.project_root = requested.resolve().parent
        config.config_path = requested.resolve()
    _validate(config)
    return config
