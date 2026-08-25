"""SQLite persistence with auditable, failure-safe board ingestion."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_key
from .models import CompanyCandidate, IngestionStats, NormalizedJob
from .normalize import ashby_board_url, canonical_json, content_hash, extract_ashby_slug, utc_now


LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 7

# Fields deliberately left out of audit snapshots.  The job row already holds
# the current payload, and repeating a full job description on every observation
# grew the database by gigabytes without adding anything auditable.
_SNAPSHOT_EXCLUDED = ("raw_data", "source_raw_json", "description_html")

# The source snapshot keeps everything the adapter did not promote to a column,
# but not the description: that is already stored verbatim in description_html
# and description_text, and it is by far the largest field on a posting.
_RAW_DESCRIPTION_FIELDS = (
    "descriptionHtml",
    "descriptionHTML",
    "descriptionPlain",
    "descriptionText",
    "description",
    "description_plain",
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    command             TEXT NOT NULL DEFAULT 'run',
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    stats_json          TEXT NOT NULL DEFAULT '{}',
    error               TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL DEFAULT 'ashby',
    slug                TEXT NOT NULL COLLATE NOCASE,
    display_name        TEXT,
    board_url           TEXT,
    first_seen_at       TEXT NOT NULL,
    last_verified_at    TEXT,
    last_success_at     TEXT,
    active              INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    discovery_source    TEXT,
    discovery_evidence  TEXT,
    discovered_run_id   INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    last_job_count      INTEGER,
    last_error          TEXT,
    UNIQUE (source, slug)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                      INTEGER PRIMARY KEY,
    company_id              INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
    source                  TEXT NOT NULL,
    source_company_slug     TEXT NOT NULL COLLATE NOCASE,
    source_job_id           TEXT NOT NULL,
    company_name            TEXT NOT NULL,
    title                   TEXT NOT NULL,
    department              TEXT,
    team                    TEXT,
    location                TEXT,
    secondary_locations_json TEXT NOT NULL DEFAULT '[]',
    workplace_type          TEXT,
    employment_type         TEXT,
    description_html        TEXT,
    description_text        TEXT NOT NULL DEFAULT '',
    published_at            TEXT,
    job_url                 TEXT,
    apply_url               TEXT,
    compensation_text       TEXT,
    compensation_json       TEXT,
    source_raw_json         TEXT NOT NULL DEFAULT '{}',
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    active                  INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    discovered_run_id       INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    last_seen_run_id        INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    content_hash            TEXT NOT NULL,
    semantic_fingerprint    TEXT,
    semantic_duplicate_key  TEXT,
    canonical_key           TEXT,
    evidence_confidence     TEXT,
    entry_level_score       INTEGER,
    experience_category     TEXT,
    experience_summary      TEXT,
    experience_detail       TEXT,
    blocker_status          TEXT,
    geo_tags_json           TEXT NOT NULL DEFAULT '[]',
    is_software             INTEGER CHECK (is_software IN (0, 1) OR is_software IS NULL),
    search_score            INTEGER CHECK (search_score BETWEEN 0 AND 100 OR search_score IS NULL),
    match_category          TEXT,
    eligibility_flags_json  TEXT NOT NULL DEFAULT '[]',
    score_reasons_json      TEXT NOT NULL DEFAULT '[]',
    authorization_flag      TEXT,
    uncertain               INTEGER NOT NULL DEFAULT 0 CHECK (uncertain IN (0, 1)),
    new_since_previous_run  INTEGER NOT NULL DEFAULT 1 CHECK (new_since_previous_run IN (0, 1)),
    updated_at              TEXT NOT NULL,
    closed_at               TEXT,
    reopened_count          INTEGER NOT NULL DEFAULT 0 CHECK (reopened_count >= 0),
    UNIQUE (source, source_company_slug, source_job_id)
);

CREATE TABLE IF NOT EXISTS discovery_events (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    source              TEXT NOT NULL,
    slug                TEXT NOT NULL COLLATE NOCASE,
    discovery_source    TEXT NOT NULL,
    discovery_evidence  TEXT,
    candidate_url       TEXT,
    discovered_at       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'candidate',
    message             TEXT
);

-- Which discovery sources produced each slug.  One row per (slug, source), so
-- the per-source and overlap figures in the discovery report are measured
-- rather than estimated, and the table stays bounded across runs.
CREATE TABLE IF NOT EXISTS discovery_slug_sources (
    id                  INTEGER PRIMARY KEY,
    source              TEXT NOT NULL DEFAULT 'ashby',
    slug                TEXT NOT NULL COLLATE NOCASE,
    discovery_source    TEXT NOT NULL,
    discovery_evidence  TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    times_seen          INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source, slug, discovery_source)
);

-- The canonicalized opportunity set the dashboard serves.
--
-- Building this list means reading every stored posting and running the
-- duplicate-collapsing pass, which takes about twenty seconds against a full
-- registry.  That is far too slow to do per request, so it is materialized
-- once whenever the reports are regenerated and then queried with plain SQL.
--
-- ``payload_json`` holds the complete card as the reports render it, so the
-- API cannot drift from what the static export shows.  Descriptions are
-- deliberately *not* stored here: the dashboard never needs them, and keeping
-- them out is what makes the table small enough to query freely.
CREATE TABLE IF NOT EXISTS canonical_opportunities (
    id                      INTEGER PRIMARY KEY,
    job_id                  INTEGER NOT NULL,
    canonical_key           TEXT,
    company                 TEXT NOT NULL DEFAULT '',
    company_key             TEXT NOT NULL DEFAULT '',
    title                   TEXT NOT NULL DEFAULT '',
    title_key               TEXT NOT NULL DEFAULT '',
    location                TEXT NOT NULL DEFAULT '',
    location_key            TEXT NOT NULL DEFAULT '',
    search_blob             TEXT NOT NULL DEFAULT '',
    match_category          TEXT NOT NULL DEFAULT '',
    search_score            INTEGER NOT NULL DEFAULT 0,
    entry_level_score       INTEGER NOT NULL DEFAULT 0,
    evidence_confidence     TEXT NOT NULL DEFAULT 'low',
    experience_category     TEXT NOT NULL DEFAULT 'experience_unknown',
    experience_bucket       TEXT NOT NULL DEFAULT 'unknown',
    blocker_status          TEXT NOT NULL DEFAULT 'clear',
    authorization_state     TEXT NOT NULL DEFAULT 'clear',
    active                  INTEGER NOT NULL DEFAULT 1,
    is_software             INTEGER NOT NULL DEFAULT 1,
    is_remote               INTEGER NOT NULL DEFAULT 0,
    new_since_previous_run  INTEGER NOT NULL DEFAULT 0,
    geo_nyc                 INTEGER NOT NULL DEFAULT 0,
    geo_new_jersey          INTEGER NOT NULL DEFAULT 0,
    geo_remote_us           INTEGER NOT NULL DEFAULT 0,
    geo_other_us            INTEGER NOT NULL DEFAULT 0,
    duplicate_count         INTEGER NOT NULL DEFAULT 1,
    compensation_value      REAL NOT NULL DEFAULT 0,
    published_epoch         INTEGER NOT NULL DEFAULT 0,
    first_seen_epoch        INTEGER NOT NULL DEFAULT 0,
    payload_json            TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS job_observations (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    run_id              INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    observed_at         TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    snapshot_json       TEXT NOT NULL,
    UNIQUE (job_id, run_id, event_type)
);

"""

# Indexes are applied after any missing columns are added, so a new index may
# safely reference a column introduced in a later schema version.
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_companies_active ON companies(active, source, slug);
CREATE INDEX IF NOT EXISTS idx_companies_verified ON companies(last_verified_at);
CREATE INDEX IF NOT EXISTS idx_jobs_default_report
    ON jobs(active, is_software, search_score DESC, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company_active ON jobs(company_id, active);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_published ON jobs(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_semantic ON jobs(semantic_duplicate_key);
CREATE INDEX IF NOT EXISTS idx_jobs_canonical ON jobs(canonical_key, active);
CREATE INDEX IF NOT EXISTS idx_jobs_experience ON jobs(experience_category, active);
CREATE INDEX IF NOT EXISTS idx_discovery_events_run ON discovery_events(run_id, discovered_at);
CREATE INDEX IF NOT EXISTS idx_discovery_slug_sources ON discovery_slug_sources(discovery_source, slug);
CREATE INDEX IF NOT EXISTS idx_observations_run ON job_observations(run_id, event_type);
CREATE INDEX IF NOT EXISTS idx_opportunities_rank
    ON canonical_opportunities(search_score DESC, published_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_facets
    ON canonical_opportunities(match_category, experience_bucket, evidence_confidence);
CREATE INDEX IF NOT EXISTS idx_opportunities_company ON canonical_opportunities(company_key);
CREATE INDEX IF NOT EXISTS idx_opportunities_job ON canonical_opportunities(job_id);
"""


def _json(value: Any, default: Any) -> str:
    return canonical_json(default if value is None else value)


def _slim_raw(value: Any) -> Any:
    """Drop description fields that are already stored in their own columns."""

    if not isinstance(value, Mapping):
        return value
    return {key: item for key, item in value.items() if key not in _RAW_DESCRIPTION_FIELDS}


def _audit_snapshot(values: Mapping[str, Any], **overrides: Any) -> dict[str, Any]:
    """Build a compact observation snapshot of what a posting looked like."""

    snapshot = {
        key: value for key, value in values.items() if key not in _SNAPSHOT_EXCLUDED
    }
    snapshot.update(overrides)
    return snapshot


def _mapping_value(value: NormalizedJob | Mapping[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


class Database:
    """Small explicit persistence API for the job-discovery pipeline.

    Every public mutation is transactional.  Most importantly, the only normal
    operation that marks missing postings inactive is ``ingest_company_jobs``;
    callers invoke it only after a source adapter returned a complete board.
    ``record_company_failure`` never changes any job row.
    """

    def __init__(self, path: str | Path = "data/jobfinder.sqlite3") -> None:
        raw_path = str(path)
        self.path = raw_path if raw_path == ":memory:" else str(Path(raw_path).expanduser())
        self._memory_connection: sqlite3.Connection | None = None

    def _new_connection(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def connect(self) -> sqlite3.Connection:
        """Return a configured connection (the caller closes file connections)."""

        if self.path == ":memory:":
            if self._memory_connection is None:
                self._memory_connection = self._new_connection()
            return self._memory_connection
        return self._new_connection()

    def close(self) -> None:
        if self._memory_connection is not None:
            self._memory_connection.close()
            self._memory_connection = None

    def __enter__(self) -> Database:
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        should_close = self.path != ":memory:"
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            if should_close:
                connection.close()

    def initialize(self) -> None:
        """Create/migrate the database to the schema supported by this version."""

        with self._connection() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {current} is newer than supported {SCHEMA_VERSION}"
                )
            connection.executescript(_SCHEMA)
            self._add_missing_columns(connection)
            connection.executescript(_INDEXES)
            if 0 < current < SCHEMA_VERSION:
                self._compact(connection, current)
                self._backfill_canonical_keys(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        """Add columns introduced after a table was first created."""

        existing = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for column, definition in (
            ("canonical_key", "TEXT"),
            ("evidence_confidence", "TEXT"),
            ("entry_level_score", "INTEGER"),
            ("experience_category", "TEXT"),
            ("experience_summary", "TEXT"),
            ("experience_detail", "TEXT"),
            ("blocker_status", "TEXT"),
            ("geo_tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if column not in existing:
                connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")

    @staticmethod
    def _backfill_canonical_keys(connection: sqlite3.Connection) -> None:
        """Compute display-canonical keys for rows stored before the column existed."""

        rows = connection.execute(
            """
            SELECT id, source_company_slug, title, location, workplace_type, employment_type
              FROM jobs WHERE canonical_key IS NULL
            """
        ).fetchall()
        if not rows:
            return
        connection.executemany(
            "UPDATE jobs SET canonical_key = ? WHERE id = ?",
            [(canonical_key(dict(row)), int(row["id"])) for row in rows],
        )
        LOGGER.info("Backfilled canonical keys for %s stored jobs", len(rows))

    @staticmethod
    def _compact(connection: sqlite3.Connection, from_version: int) -> None:
        """Release duplicated storage written by older schema versions.

        Version 1 kept a complete job payload for every job on every run, and
        version 2 still kept a second copy of each description inside the source
        snapshot.  Both made the file grow by hundreds of megabytes per run.  No
        job, company, or timestamp is touched here — only duplicated bytes.
        """

        def measure() -> tuple[int, int]:
            observations, snapshot_bytes = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(snapshot_json)), 0) FROM job_observations"
            ).fetchone()
            raw_bytes = connection.execute(
                "SELECT COALESCE(SUM(LENGTH(source_raw_json)), 0) FROM jobs"
            ).fetchone()[0]
            return int(observations), int(snapshot_bytes) + int(raw_bytes)

        before_count, before_bytes = measure()
        if from_version < 2:
            connection.execute("DELETE FROM job_observations WHERE event_type = 'seen'")
            removals = ", ".join(f"'$.{field}'" for field in _SNAPSHOT_EXCLUDED)
            connection.execute(
                f"UPDATE job_observations SET snapshot_json = "
                f"json_remove(snapshot_json, {removals}) WHERE json_valid(snapshot_json)"
            )
        if from_version < 3:
            removals = ", ".join(f"'$.{field}'" for field in _RAW_DESCRIPTION_FIELDS)
            connection.execute(
                f"UPDATE jobs SET source_raw_json = "
                f"json_remove(source_raw_json, {removals}) WHERE json_valid(source_raw_json)"
            )
        after_count, after_bytes = measure()
        if (before_count, before_bytes) == (after_count, after_bytes):
            return
        LOGGER.info(
            "Compacted stored history: %s -> %s observations, %.0f MB -> %.0f MB duplicated data",
            before_count,
            after_count,
            before_bytes / 1048576,
            after_bytes / 1048576,
        )
        connection.execute("VACUUM")

    # A familiar alias for callers that describe initialization as migration.
    migrate = initialize

    def start_run(
        self,
        command: str = "run",
        metadata: Mapping[str, Any] | None = None,
        *,
        started_at: str | None = None,
    ) -> int:
        now = started_at or utc_now()
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs(started_at, status, command, metadata_json)
                VALUES (?, 'running', ?, ?)
                """,
                (now, command, _json(metadata, {})),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str = "success",
        stats: Mapping[str, Any] | None = None,
        error: str | None = None,
        *,
        completed_at: str | None = None,
    ) -> None:
        if not status.strip():
            raise ValueError("run status cannot be empty")
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                   SET completed_at = ?, status = ?, stats_json = ?, error = ?
                 WHERE id = ?
                """,
                (completed_at or utc_now(), status, _json(stats, {}), error, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown run id {run_id}")

    @staticmethod
    def _normalize_company_slug(slug: str, source: str) -> str:
        candidate = slug.strip()
        if source == "ashby":
            normalized = extract_ashby_slug(candidate)
            if normalized is None:
                raise ValueError(f"Invalid Ashby company slug or URL: {slug!r}")
            return normalized
        if not candidate or "/" in candidate or "\\" in candidate:
            raise ValueError(f"Invalid company slug: {slug!r}")
        return candidate

    @staticmethod
    def _default_board_url(slug: str, source: str) -> str | None:
        return ashby_board_url(slug) if source == "ashby" else None

    def _upsert_company_conn(
        self,
        connection: sqlite3.Connection,
        slug: str,
        *,
        source: str,
        display_name: str | None,
        board_url: str | None,
        discovery_source: str | None,
        discovery_evidence: str | None,
        run_id: int | None,
        seen_at: str,
        verified: bool,
    ) -> int:
        connection.execute(
            """
            INSERT INTO companies(
                source, slug, display_name, board_url, first_seen_at,
                last_verified_at, active, discovery_source, discovery_evidence,
                discovered_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, slug) DO UPDATE SET
                display_name = COALESCE(NULLIF(excluded.display_name, ''), companies.display_name),
                board_url = COALESCE(excluded.board_url, companies.board_url),
                last_verified_at = CASE
                    WHEN ? THEN excluded.last_verified_at ELSE companies.last_verified_at END,
                active = CASE WHEN ? THEN 1 ELSE companies.active END,
                consecutive_failures = CASE
                    WHEN ? THEN 0 ELSE companies.consecutive_failures END,
                last_error = CASE WHEN ? THEN NULL ELSE companies.last_error END,
                discovery_source = COALESCE(companies.discovery_source, excluded.discovery_source),
                discovery_evidence = COALESCE(
                    companies.discovery_evidence, excluded.discovery_evidence),
                discovered_run_id = COALESCE(
                    companies.discovered_run_id, excluded.discovered_run_id)
            """,
            (
                source,
                slug,
                display_name,
                board_url,
                seen_at,
                seen_at if verified else None,
                int(verified),
                discovery_source,
                discovery_evidence,
                run_id,
                int(verified),
                int(verified),
                int(verified),
                int(verified),
            ),
        )
        row = connection.execute(
            "SELECT id FROM companies WHERE source = ? AND slug = ?", (source, slug)
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite would have failed first
            raise RuntimeError("company upsert did not return a row")
        return int(row["id"])

    def upsert_company(
        self,
        slug: str,
        *,
        source: str = "ashby",
        display_name: str | None = None,
        board_url: str | None = None,
        discovery_source: str | None = None,
        discovery_evidence: str | None = None,
        run_id: int | None = None,
        seen_at: str | None = None,
        verified: bool = False,
    ) -> int:
        normalized = self._normalize_company_slug(slug, source)
        now = seen_at or utc_now()
        with self._connection(write=True) as connection:
            return self._upsert_company_conn(
                connection,
                normalized,
                source=source,
                display_name=display_name,
                board_url=board_url or self._default_board_url(normalized, source),
                discovery_source=discovery_source,
                discovery_evidence=discovery_evidence,
                run_id=run_id,
                seen_at=now,
                verified=verified,
            )

    def register_company_candidate(
        self,
        candidate: CompanyCandidate | object,
        *,
        run_id: int | None = None,
        status: str = "candidate",
        message: str | None = None,
        discovered_at: str | None = None,
    ) -> int:
        """Persist a candidate and its provenance together."""

        # Native CompanyCandidate separates ATS source from discovery provenance.
        # The discovery package's lightweight candidate instead calls provenance
        # ``source`` and evidence ``evidence``; accept both without coupling the
        # persistence module to a concrete discovery provider.
        if isinstance(candidate, CompanyCandidate):
            source = candidate.source
            raw_slug = candidate.slug
            provenance = candidate.discovery_source
            evidence = candidate.discovery_evidence
            candidate_url = candidate.candidate_url
        else:
            raw_slug = str(getattr(candidate, "slug"))
            source = "ashby"
            provenance = str(
                getattr(candidate, "discovery_source", None) or getattr(candidate, "source")
            )
            evidence = getattr(candidate, "discovery_evidence", None) or getattr(
                candidate, "evidence", None
            )
            candidate_url = getattr(candidate, "candidate_url", None)
        slug = self._normalize_company_slug(raw_slug, source)
        now = discovered_at or utc_now()
        with self._connection(write=True) as connection:
            company_id = self._upsert_company_conn(
                connection,
                slug,
                source=source,
                display_name=None,
                board_url=self._default_board_url(slug, source) or candidate_url,
                discovery_source=provenance,
                discovery_evidence=evidence,
                run_id=run_id,
                seen_at=now,
                verified=False,
            )
            connection.execute(
                """
                INSERT INTO discovery_events(
                    run_id, source, slug, discovery_source, discovery_evidence,
                    candidate_url, discovered_at, status, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    slug,
                    provenance,
                    evidence,
                    candidate_url,
                    now,
                    status,
                    message,
                ),
            )
            return company_id

    # Compatibility name used naturally by discovery orchestrators.
    record_company_candidate = register_company_candidate

    def record_discovery_event(
        self,
        slug: str,
        discovery_source: str,
        *,
        source: str = "ashby",
        discovery_evidence: str | None = None,
        candidate_url: str | None = None,
        run_id: int | None = None,
        status: str = "candidate",
        message: str | None = None,
        discovered_at: str | None = None,
    ) -> int:
        candidate = CompanyCandidate(
            slug=slug,
            source=source,
            discovery_source=discovery_source,
            discovery_evidence=discovery_evidence,
            candidate_url=candidate_url,
        )
        return self.register_company_candidate(
            candidate,
            run_id=run_id,
            status=status,
            message=message,
            discovered_at=discovered_at,
        )

    def record_slug_sources(
        self,
        entries: Iterable[tuple[str, str, str | None]],
        *,
        source: str = "ashby",
        seen_at: str | None = None,
    ) -> int:
        """Record which discovery source produced which slug.

        One row per ``(slug, discovery_source)`` pair, so a slug found by three
        sources leaves three rows and the overlap between sources is a plain
        ``GROUP BY`` rather than an estimate.  Repeated sightings only bump a
        counter, which keeps the table bounded no matter how often it runs.
        """

        now = seen_at or utc_now()
        prepared = []
        for slug, discovery_source, evidence in entries:
            normalized = str(slug or "").strip()
            if not normalized or not discovery_source:
                continue
            prepared.append((source, normalized, discovery_source, evidence, now, now))
        if not prepared:
            return 0
        with self._connection(write=True) as connection:
            connection.executemany(
                """
                INSERT INTO discovery_slug_sources(
                    source, slug, discovery_source, discovery_evidence,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, slug, discovery_source) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    times_seen = times_seen + 1,
                    discovery_evidence = COALESCE(
                        excluded.discovery_evidence, discovery_evidence
                    )
                """,
                prepared,
            )
        return len(prepared)

    def discovery_source_stats(self, *, source: str = "ashby") -> list[dict[str, Any]]:
        """Per-source slug counts, joined against what actually verified."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT d.discovery_source                                AS discovery_source,
                       COUNT(DISTINCT d.slug)                            AS unique_slugs,
                       COUNT(DISTINCT CASE WHEN c.id IS NOT NULL
                                           THEN d.slug END)              AS registered,
                       COUNT(DISTINCT CASE WHEN c.last_success_at IS NOT NULL
                                           THEN d.slug END)              AS verified,
                       COUNT(DISTINCT CASE WHEN c.active = 1
                                           THEN d.slug END)              AS active_boards,
                       MIN(d.first_seen_at)                              AS first_seen_at,
                       MAX(d.last_seen_at)                               AS last_seen_at
                  FROM discovery_slug_sources d
                  LEFT JOIN companies c
                         ON c.source = d.source AND c.slug = d.slug
                 WHERE d.source = ?
                 GROUP BY d.discovery_source
                 ORDER BY unique_slugs DESC
                """,
                (source,),
            ).fetchall()
        return [dict(row) for row in rows]

    def discovery_source_overlap(self, *, source: str = "ashby") -> dict[str, Any]:
        """How much each pair of sources agrees, and how many slugs are unique.

        ``exclusive`` counts slugs only one source ever reported, which is the
        number that says whether adding a source was worth it.
        """

        with self._connection() as connection:
            pairs = connection.execute(
                """
                SELECT a.discovery_source AS left_source,
                       b.discovery_source AS right_source,
                       COUNT(DISTINCT a.slug) AS shared
                  FROM discovery_slug_sources a
                  JOIN discovery_slug_sources b
                    ON a.source = b.source AND a.slug = b.slug
                   AND a.discovery_source < b.discovery_source
                 WHERE a.source = ?
                 GROUP BY a.discovery_source, b.discovery_source
                 ORDER BY shared DESC
                """,
                (source,),
            ).fetchall()
            exclusive = connection.execute(
                """
                SELECT discovery_source, COUNT(*) AS exclusive_slugs FROM (
                    SELECT slug, MIN(discovery_source) AS discovery_source
                      FROM discovery_slug_sources
                     WHERE source = ?
                     GROUP BY slug
                    HAVING COUNT(DISTINCT discovery_source) = 1
                ) GROUP BY discovery_source ORDER BY exclusive_slugs DESC
                """,
                (source,),
            ).fetchall()
            total = connection.execute(
                "SELECT COUNT(DISTINCT slug) FROM discovery_slug_sources WHERE source = ?",
                (source,),
            ).fetchone()[0]
        return {
            "total_unique_slugs": int(total or 0),
            "pairs": [dict(row) for row in pairs],
            "exclusive": {
                str(row["discovery_source"]): int(row["exclusive_slugs"]) for row in exclusive
            },
        }

    def record_company_success(
        self,
        slug: str,
        *,
        source: str = "ashby",
        display_name: str | None = None,
        board_url: str | None = None,
        job_count: int | None = None,
        succeeded_at: str | None = None,
    ) -> int:
        """Record validation/fetch success without changing job activity.

        Full board ingestion should normally use :meth:`ingest_company_jobs`, which
        performs this bookkeeping and current-job reconciliation atomically.
        """

        normalized = self._normalize_company_slug(slug, source)
        now = succeeded_at or utc_now()
        with self._connection(write=True) as connection:
            company_id = self._upsert_company_conn(
                connection,
                normalized,
                source=source,
                display_name=display_name,
                board_url=board_url or self._default_board_url(normalized, source),
                discovery_source=None,
                discovery_evidence=None,
                run_id=None,
                seen_at=now,
                verified=True,
            )
            connection.execute(
                """
                UPDATE companies
                   SET last_success_at = ?, last_job_count = COALESCE(?, last_job_count),
                       active = 1, consecutive_failures = 0, last_error = NULL
                 WHERE id = ?
                """,
                (now, job_count, company_id),
            )
            return company_id

    def record_company_failure(
        self,
        slug: str,
        error: str,
        *,
        source: str = "ashby",
        failed_at: str | None = None,
        inactive_after: int = 3,
    ) -> int:
        """Track a failed board request; never modify job activity."""

        if inactive_after < 1:
            raise ValueError("inactive_after must be at least 1")
        normalized = self._normalize_company_slug(slug, source)
        now = failed_at or utc_now()
        with self._connection(write=True) as connection:
            company_id = self._upsert_company_conn(
                connection,
                normalized,
                source=source,
                display_name=None,
                board_url=self._default_board_url(normalized, source),
                discovery_source=None,
                discovery_evidence=None,
                run_id=None,
                seen_at=now,
                verified=False,
            )
            connection.execute(
                """
                UPDATE companies
                   SET last_verified_at = ?,
                       consecutive_failures = consecutive_failures + 1,
                       active = CASE
                           WHEN consecutive_failures + 1 >= ? THEN 0 ELSE active END,
                       last_error = ?
                 WHERE id = ?
                """,
                (now, inactive_after, error, company_id),
            )
            return company_id

    @staticmethod
    def _validate_job(
        job: NormalizedJob | Mapping[str, Any], source: str, slug: str
    ) -> tuple[str, int | None]:
        job_source = str(_mapping_value(job, "source", source))
        job_slug = str(_mapping_value(job, "source_company_slug", slug))
        source_job_id = str(_mapping_value(job, "source_job_id", "")).strip()
        title = str(_mapping_value(job, "title", "")).strip()
        if job_source != source or job_slug.casefold() != slug.casefold():
            raise ValueError(f"Job {source_job_id!r} belongs to a different source/board")
        if not source_job_id or not title:
            raise ValueError("Every normalized job requires source_job_id and title")
        score_value = _mapping_value(job, "search_score")
        score = None if score_value is None else int(score_value)
        if score is not None and not 0 <= score <= 100:
            raise ValueError(f"Job {source_job_id!r} score must be between 0 and 100")
        return source_job_id, score

    @staticmethod
    def _job_values(
        job: NormalizedJob | Mapping[str, Any],
        *,
        source: str,
        slug: str,
        score: int | None,
    ) -> dict[str, Any]:
        snapshot = job.snapshot() if isinstance(job, NormalizedJob) else dict(job)
        computed_hash = str(_mapping_value(job, "content_hash", "")).strip() or content_hash(
            {
                key: snapshot.get(key)
                for key in (
                    "title",
                    "department",
                    "team",
                    "location",
                    "secondary_locations",
                    "workplace_type",
                    "employment_type",
                    "description_html",
                    "description_text",
                    "published_at",
                    "job_url",
                    "apply_url",
                    "compensation",
                    "compensation_text",
                )
            }
        )
        category = _mapping_value(job, "eligibility_category") or _mapping_value(
            job, "match_category"
        )
        semantic_key = _mapping_value(job, "semantic_duplicate_key") or _mapping_value(
            job, "semantic_fingerprint"
        )
        company_name = str(_mapping_value(job, "company_name", slug)).strip() or slug
        return {
            "source": source,
            "slug": slug,
            "source_job_id": str(_mapping_value(job, "source_job_id")).strip(),
            "company_name": company_name,
            "title": str(_mapping_value(job, "title")).strip(),
            "department": _mapping_value(job, "department"),
            "team": _mapping_value(job, "team"),
            "location": _mapping_value(job, "location"),
            "secondary_locations_json": _json(_mapping_value(job, "secondary_locations", ()), []),
            "workplace_type": _mapping_value(job, "workplace_type"),
            "employment_type": _mapping_value(job, "employment_type"),
            "description_html": _mapping_value(job, "description_html"),
            "description_text": str(_mapping_value(job, "description_text", "") or ""),
            "published_at": _mapping_value(job, "published_at"),
            "job_url": _mapping_value(job, "job_url"),
            "apply_url": _mapping_value(job, "apply_url"),
            "compensation_text": _mapping_value(job, "compensation_text"),
            "compensation_json": (
                None
                if _mapping_value(job, "compensation") is None
                else _json(_mapping_value(job, "compensation"), {})
            ),
            "source_raw_json": _json(_slim_raw(_mapping_value(job, "raw_data", {})), {}),
            "content_hash": computed_hash,
            "semantic_fingerprint": _mapping_value(job, "semantic_fingerprint"),
            "semantic_duplicate_key": semantic_key,
            # Bucket key for the display-canonicalization layer.  Stored so the
            # collapsing done in the reports can be inspected with plain SQL.
            "canonical_key": canonical_key(
                {
                    "source_company_slug": slug,
                    "title": _mapping_value(job, "title"),
                    "location": _mapping_value(job, "location"),
                    "workplace_type": _mapping_value(job, "workplace_type"),
                    "employment_type": _mapping_value(job, "employment_type"),
                }
            ),
            "is_software": (
                None
                if _mapping_value(job, "is_software") is None
                else int(bool(_mapping_value(job, "is_software")))
            ),
            "search_score": score,
            "match_category": category,
            "eligibility_flags_json": _json(_mapping_value(job, "eligibility_flags", ()), []),
            "score_reasons_json": _json(_mapping_value(job, "score_reasons", ()), []),
            "authorization_flag": _mapping_value(job, "authorization_flag"),
            "uncertain": int(bool(_mapping_value(job, "uncertain", False))),
            "evidence_confidence": _mapping_value(job, "evidence_confidence") or "low",
            "entry_level_score": _mapping_value(job, "entry_level_score"),
            "experience_category": (
                _mapping_value(job, "experience_category") or "experience_unknown"
            ),
            "experience_summary": _mapping_value(job, "experience_summary") or "Not stated",
            "experience_detail": _mapping_value(job, "experience_detail") or "",
            "blocker_status": _mapping_value(job, "blocker_status") or "clear",
            "geo_tags_json": _json(_mapping_value(job, "geo_tags", ()), []),
            "snapshot": snapshot,
        }

    def ingest_company_jobs(
        self,
        slug: str,
        jobs: Sequence[NormalizedJob | Mapping[str, Any]]
        | Iterable[NormalizedJob | Mapping[str, Any]],
        run_id: int | None,
        *,
        source: str = "ashby",
        display_name: str | None = None,
        board_url: str | None = None,
        fetched_at: str | None = None,
    ) -> IngestionStats:
        """Atomically reconcile one *successfully fetched, complete* board.

        The iterable is fully validated before the transaction.  If any item is
        malformed, nothing is written and existing jobs remain active.
        """

        normalized_slug = self._normalize_company_slug(slug, source)
        materialized = list(jobs)
        prepared: list[dict[str, Any]] = []
        source_ids: set[str] = set()
        for job in materialized:
            source_job_id, score = self._validate_job(job, source, normalized_slug)
            if source_job_id in source_ids:
                raise ValueError(f"Duplicate job id {source_job_id!r} in board response")
            source_ids.add(source_job_id)
            prepared.append(self._job_values(job, source=source, slug=normalized_slug, score=score))

        now = fetched_at or utc_now()
        inferred_name = display_name
        if not inferred_name and prepared:
            inferred_name = str(prepared[0]["company_name"])
        with self._connection(write=True) as connection:
            if run_id is not None and connection.execute(
                "SELECT 1 FROM runs WHERE id = ?", (run_id,)
            ).fetchone() is None:
                raise KeyError(f"Unknown run id {run_id}")
            company_id = self._upsert_company_conn(
                connection,
                normalized_slug,
                source=source,
                display_name=inferred_name,
                board_url=board_url or self._default_board_url(normalized_slug, source),
                discovery_source=None,
                discovery_evidence=None,
                run_id=run_id,
                seen_at=now,
                verified=True,
            )
            counts = {
                "seen": len(prepared),
                "inserted": 0,
                "updated": 0,
                "reopened": 0,
                "unchanged": 0,
                "closed": 0,
            }

            for values in prepared:
                existing = connection.execute(
                    """
                    SELECT id, active, content_hash, discovered_run_id
                      FROM jobs
                     WHERE source = ? AND source_company_slug = ? AND source_job_id = ?
                    """,
                    (source, normalized_slug, values["source_job_id"]),
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO jobs(
                            company_id, source, source_company_slug, source_job_id,
                            company_name, title, department, team, location,
                            secondary_locations_json, workplace_type, employment_type,
                            description_html, description_text, published_at, job_url,
                            apply_url, compensation_text, compensation_json, source_raw_json,
                            first_seen_at, last_seen_at, active, discovered_run_id,
                            last_seen_run_id, content_hash, semantic_fingerprint,
                            semantic_duplicate_key, canonical_key, is_software, search_score,
                            match_category, eligibility_flags_json, score_reasons_json,
                            authorization_flag, uncertain, evidence_confidence,
                            entry_level_score, experience_category, experience_summary,
                            experience_detail, blocker_status, geo_tags_json,
                            new_since_previous_run, updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, 1, ?
                        )
                        """,
                        (
                            company_id,
                            source,
                            normalized_slug,
                            values["source_job_id"],
                            values["company_name"],
                            values["title"],
                            values["department"],
                            values["team"],
                            values["location"],
                            values["secondary_locations_json"],
                            values["workplace_type"],
                            values["employment_type"],
                            values["description_html"],
                            values["description_text"],
                            values["published_at"],
                            values["job_url"],
                            values["apply_url"],
                            values["compensation_text"],
                            values["compensation_json"],
                            values["source_raw_json"],
                            now,
                            now,
                            run_id,
                            run_id,
                            values["content_hash"],
                            values["semantic_fingerprint"],
                            values["semantic_duplicate_key"],
                            values["canonical_key"],
                            values["is_software"],
                            values["search_score"],
                            values["match_category"],
                            values["eligibility_flags_json"],
                            values["score_reasons_json"],
                            values["authorization_flag"],
                            values["uncertain"],
                            values["evidence_confidence"],
                            values["entry_level_score"],
                            values["experience_category"],
                            values["experience_summary"],
                            values["experience_detail"],
                            values["blocker_status"],
                            values["geo_tags_json"],
                            now,
                        ),
                    )
                    job_id = int(cursor.lastrowid)
                    event_type = "new"
                    counts["inserted"] += 1
                else:
                    job_id = int(existing["id"])
                    was_active = bool(existing["active"])
                    changed = existing["content_hash"] != values["content_hash"]
                    if not was_active:
                        event_type = "reopened"
                        counts["reopened"] += 1
                    elif changed:
                        event_type = "updated"
                        counts["updated"] += 1
                    else:
                        event_type = "seen"
                        counts["unchanged"] += 1
                    new_flag = int(
                        run_id is not None and existing["discovered_run_id"] == run_id
                    )
                    connection.execute(
                        """
                        UPDATE jobs SET
                            company_id = ?, company_name = ?, title = ?, department = ?,
                            team = ?, location = ?, secondary_locations_json = ?,
                            workplace_type = ?, employment_type = ?, description_html = ?,
                            description_text = ?, published_at = ?, job_url = ?, apply_url = ?,
                            compensation_text = ?, compensation_json = ?, source_raw_json = ?,
                            last_seen_at = ?, active = 1, last_seen_run_id = ?, content_hash = ?,
                            semantic_fingerprint = ?, semantic_duplicate_key = ?,
                            canonical_key = ?,
                            is_software = ?, search_score = ?, match_category = ?,
                            eligibility_flags_json = ?, score_reasons_json = ?,
                            authorization_flag = ?, uncertain = ?, evidence_confidence = ?,
                            entry_level_score = ?, experience_category = ?,
                            experience_summary = ?, experience_detail = ?,
                            blocker_status = ?, geo_tags_json = ?,
                            new_since_previous_run = ?,
                            updated_at = CASE WHEN content_hash <> ? THEN ? ELSE updated_at END,
                            closed_at = NULL,
                            reopened_count = reopened_count + ?
                        WHERE id = ?
                        """,
                        (
                            company_id,
                            values["company_name"],
                            values["title"],
                            values["department"],
                            values["team"],
                            values["location"],
                            values["secondary_locations_json"],
                            values["workplace_type"],
                            values["employment_type"],
                            values["description_html"],
                            values["description_text"],
                            values["published_at"],
                            values["job_url"],
                            values["apply_url"],
                            values["compensation_text"],
                            values["compensation_json"],
                            values["source_raw_json"],
                            now,
                            run_id,
                            values["content_hash"],
                            values["semantic_fingerprint"],
                            values["semantic_duplicate_key"],
                            values["canonical_key"],
                            values["is_software"],
                            values["search_score"],
                            values["match_category"],
                            values["eligibility_flags_json"],
                            values["score_reasons_json"],
                            values["authorization_flag"],
                            values["uncertain"],
                            values["evidence_confidence"],
                            values["entry_level_score"],
                            values["experience_category"],
                            values["experience_summary"],
                            values["experience_detail"],
                            values["blocker_status"],
                            values["geo_tags_json"],
                            new_flag,
                            values["content_hash"],
                            now,
                            int(not was_active),
                            job_id,
                        ),
                    )

                # An unchanged sighting is already recorded by the job's own
                # last_seen_at/last_seen_run_id.  Writing one audit row per job
                # per run on top of that adds nothing and grows without bound.
                if event_type != "seen":
                    connection.execute(
                        """
                        INSERT INTO job_observations(
                            job_id, run_id, observed_at, event_type, content_hash, snapshot_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(job_id, run_id, event_type) DO NOTHING
                        """,
                        (
                            job_id,
                            run_id,
                            now,
                            event_type,
                            values["content_hash"],
                            _json(_audit_snapshot(values["snapshot"], active=True), {}),
                        ),
                    )

            active_rows = connection.execute(
                "SELECT * FROM jobs WHERE company_id = ? AND active = 1", (company_id,)
            ).fetchall()
            for row in active_rows:
                if row["source_job_id"] in source_ids:
                    continue
                job_id = int(row["id"])
                connection.execute(
                    """
                    UPDATE jobs
                       SET active = 0, closed_at = ?, new_since_previous_run = 0
                     WHERE id = ?
                    """,
                    (now, job_id),
                )
                closed_snapshot = _audit_snapshot(dict(row), active=False, closed_at=now)
                connection.execute(
                    """
                    INSERT INTO job_observations(
                        job_id, run_id, observed_at, event_type, content_hash, snapshot_json
                    ) VALUES (?, ?, ?, 'closed', ?, ?)
                    ON CONFLICT(job_id, run_id, event_type) DO NOTHING
                    """,
                    (job_id, run_id, now, row["content_hash"], _json(closed_snapshot, {})),
                )
                counts["closed"] += 1

            connection.execute(
                """
                UPDATE companies
                   SET display_name = COALESCE(?, display_name), board_url = COALESCE(?, board_url),
                       last_verified_at = ?, last_success_at = ?, active = 1,
                       consecutive_failures = 0, last_error = NULL, last_job_count = ?
                 WHERE id = ?
                """,
                (
                    inferred_name,
                    board_url or self._default_board_url(normalized_slug, source),
                    now,
                    now,
                    len(prepared),
                    company_id,
                ),
            )

        return IngestionStats(company_id=company_id, **counts)

    def update_job_analysis(
        self,
        job_id: int,
        *,
        is_software: bool | None = None,
        search_score: int | None = None,
        eligibility_category: str | None = None,
        eligibility_flags: Sequence[str] | None = None,
        score_reasons: Sequence[Mapping[str, Any]] | None = None,
        authorization_flag: str | None = None,
        uncertain: bool | None = None,
        semantic_duplicate_key: str | None = None,
    ) -> None:
        """Update derived analysis independently when a re-score is needed."""

        if search_score is not None and not 0 <= search_score <= 100:
            raise ValueError("search_score must be between 0 and 100")
        assignments: list[str] = []
        parameters: list[Any] = []
        provided = {
            "is_software": None if is_software is None else int(is_software),
            "search_score": search_score,
            "match_category": eligibility_category,
            "eligibility_flags_json": (
                None if eligibility_flags is None else _json(eligibility_flags, [])
            ),
            "score_reasons_json": None if score_reasons is None else _json(score_reasons, []),
            "authorization_flag": authorization_flag,
            "uncertain": None if uncertain is None else int(uncertain),
            "semantic_duplicate_key": semantic_duplicate_key,
        }
        for column, value in provided.items():
            if value is not None:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        parameters.extend((utc_now(), job_id))
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", parameters
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown job id {job_id}")

    def update_job_analyses(self, updates: Iterable[Mapping[str, Any]]) -> int:
        """Rewrite derived analysis for many jobs in one transaction.

        Only scoring output is touched; the source snapshot, timestamps, and
        activity of each posting are left exactly as they were.  This is what
        makes it safe to re-run evolving heuristics over stored history.
        """

        prepared: list[tuple[Any, ...]] = []
        for update in updates:
            job_id = int(update["job_id"])
            score = update.get("search_score")
            if score is not None:
                score = int(score)
                if not 0 <= score <= 100:
                    raise ValueError(f"Job {job_id} score must be between 0 and 100")
            entry_level = update.get("entry_level_score")
            if entry_level is not None:
                entry_level = int(entry_level)
                if not 0 <= entry_level <= 100:
                    raise ValueError(
                        f"Job {job_id} entry_level_score must be between 0 and 100"
                    )
            is_software = update.get("is_software")
            prepared.append(
                (
                    None if is_software is None else int(bool(is_software)),
                    score,
                    update.get("eligibility_category") or update.get("match_category"),
                    _json(update.get("eligibility_flags", ()), []),
                    _json(update.get("score_reasons", ()), []),
                    update.get("authorization_flag"),
                    int(bool(update.get("uncertain", False))),
                    update.get("semantic_duplicate_key"),
                    update.get("evidence_confidence") or "low",
                    entry_level,
                    update.get("experience_category") or "experience_unknown",
                    update.get("experience_summary") or "Not stated",
                    update.get("experience_detail") or "",
                    update.get("blocker_status") or "clear",
                    _json(update.get("geo_tags", ()), []),
                    job_id,
                )
            )
        if not prepared:
            return 0
        with self._connection(write=True) as connection:
            connection.executemany(
                """
                UPDATE jobs SET
                    is_software = ?, search_score = ?, match_category = ?,
                    eligibility_flags_json = ?, score_reasons_json = ?,
                    authorization_flag = ?, uncertain = ?,
                    semantic_duplicate_key = COALESCE(?, semantic_duplicate_key),
                    evidence_confidence = ?, entry_level_score = ?,
                    experience_category = ?, experience_summary = ?,
                    experience_detail = ?, blocker_status = ?, geo_tags_json = ?
                WHERE id = ?
                """,
                prepared,
            )
        return len(prepared)

    @staticmethod
    def _decoded_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        json_fields = {
            "secondary_locations_json": ("secondary_locations", []),
            "compensation_json": ("compensation", None),
            "source_raw_json": ("raw_data", {}),
            "eligibility_flags_json": ("eligibility_flags", []),
            "score_reasons_json": ("score_reasons", []),
            "geo_tags_json": ("geo_tags", []),
            "metadata_json": ("metadata", {}),
            "stats_json": ("stats", {}),
            "snapshot_json": ("snapshot", {}),
        }
        for source_name, (target_name, default) in json_fields.items():
            if source_name not in result:
                continue
            raw = result[source_name]
            try:
                result[target_name] = default if raw is None else json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                result[target_name] = default
        for key in ("active", "is_software", "uncertain", "new_since_previous_run"):
            if key in result and result[key] is not None:
                result[key] = bool(result[key])
        if "match_category" in result:
            result["eligibility_category"] = result["match_category"]
        if "semantic_duplicate_key" not in result or not result["semantic_duplicate_key"]:
            result["semantic_duplicate_key"] = result.get("semantic_fingerprint")
        return result

    def get_company(self, slug: str, *, source: str = "ashby") -> dict[str, Any] | None:
        normalized = self._normalize_company_slug(slug, source)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM companies WHERE source = ? AND slug = ?", (source, normalized)
            ).fetchone()
        return None if row is None else self._decoded_row(row)

    def list_companies(
        self,
        *,
        source: str | None = None,
        active: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if source is not None:
            clauses.append("source = ?")
            parameters.append(source)
        if active is not None:
            clauses.append("active = ?")
            parameters.append(int(active))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM companies{where} ORDER BY COALESCE(display_name, slug), slug",
                parameters,
            ).fetchall()
        return [self._decoded_row(row) for row in rows]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else self._decoded_row(row)

    def list_jobs(
        self,
        *,
        active: bool | None = True,
        is_software: bool | None = None,
        min_score: int | None = None,
        match_category: str | None = None,
        new_only: bool = False,
        published_since: str | None = None,
        first_seen_since: str | None = None,
        company_slug: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if active is not None:
            clauses.append("active = ?")
            parameters.append(int(active))
        if is_software is not None:
            clauses.append("is_software = ?")
            parameters.append(int(is_software))
        if min_score is not None:
            clauses.append("search_score >= ?")
            parameters.append(min_score)
        if match_category is not None:
            clauses.append("match_category = ?")
            parameters.append(match_category)
        if new_only:
            clauses.append("new_since_previous_run = 1")
        if published_since is not None:
            clauses.append("published_at >= ?")
            parameters.append(published_since)
        if first_seen_since is not None:
            clauses.append("first_seen_at >= ?")
            parameters.append(first_seen_since)
        if company_slug is not None:
            clauses.append("source_company_slug = ?")
            parameters.append(company_slug)
        if limit is not None and limit < 0:
            raise ValueError("limit cannot be negative")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs{where}
                 ORDER BY COALESCE(search_score, -1) DESC,
                          COALESCE(published_at, first_seen_at) DESC,
                          first_seen_at DESC, id DESC{limit_sql}
                """,
                parameters,
            ).fetchall()
        return [self._decoded_row(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else self._decoded_row(row)

    def list_observations(
        self, *, job_id: int | None = None, run_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if job_id is not None:
            clauses.append("job_id = ?")
            parameters.append(job_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM job_observations{where} ORDER BY observed_at, id", parameters
            ).fetchall()
        return [self._decoded_row(row) for row in rows]

    def summary_counts(self, run_id: int | None = None) -> dict[str, int]:
        """Return actual persisted counts used by the CLI run summary."""

        with self._connection() as connection:
            scalar = lambda sql, params=(): int(connection.execute(sql, params).fetchone()[0])
            counts = {
                "companies_known": scalar("SELECT COUNT(*) FROM companies"),
                "companies_verified": scalar(
                    "SELECT COUNT(*) FROM companies WHERE last_success_at IS NOT NULL"
                ),
                "active_jobs": scalar("SELECT COUNT(*) FROM jobs WHERE active = 1"),
                "software_jobs": scalar(
                    "SELECT COUNT(*) FROM jobs WHERE active = 1 AND is_software = 1"
                ),
                "likely_entry_level": scalar(
                    "SELECT COUNT(*) FROM jobs WHERE active = 1 AND is_software = 1 "
                    "AND search_score >= 45 AND match_category <> 'likely_ineligible'"
                ),
                "strong_matches": scalar(
                    "SELECT COUNT(*) FROM jobs WHERE active = 1 AND is_software = 1 "
                    "AND match_category = 'strong_match'"
                ),
            }
            if run_id is None:
                counts["companies_newly_discovered"] = 0
                counts["new_strong_matches"] = 0
            else:
                counts["companies_newly_discovered"] = scalar(
                    "SELECT COUNT(*) FROM companies WHERE discovered_run_id = ?", (run_id,)
                )
                counts["new_strong_matches"] = scalar(
                    "SELECT COUNT(*) FROM jobs WHERE active = 1 AND is_software = 1 "
                    "AND match_category = 'strong_match' AND discovered_run_id = ?",
                    (run_id,),
                )
        return counts

    # ------------------------------------------------------------------
    # Canonical opportunities: the set the local dashboard queries.
    # ------------------------------------------------------------------

    #: Sort keys the API accepts, mapped to SQL.  A closed allow-list means an
    #: unknown ``sort`` value can never reach the query.
    SORT_ORDERS: dict[str, str] = {
        "best": "search_score DESC, MAX(published_epoch, first_seen_epoch) DESC, id ASC",
        "published": "published_epoch DESC, search_score DESC, id ASC",
        "discovered": "first_seen_epoch DESC, search_score DESC, id ASC",
        "company": "company_key ASC, search_score DESC, id ASC",
        "compensation": "compensation_value DESC, search_score DESC, id ASC",
        "entry_level": "entry_level_score DESC, search_score DESC, id ASC",
    }
    PAGE_SIZES: tuple[int, ...] = (25, 50, 100)
    DEFAULT_PAGE_SIZE = 50

    def replace_opportunities(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Rewrite the materialized opportunity set in one transaction.

        Callers pass rows that already went through canonicalization and the
        report normalizer, so the dashboard and the CSV describe exactly the
        same opportunities.
        """

        prepared = [
            (
                int(row.get("job_id") or 0),
                row.get("canonical_key"),
                str(row.get("company") or ""),
                str(row.get("company") or "").casefold(),
                str(row.get("title") or ""),
                str(row.get("title") or "").casefold(),
                str(row.get("location") or ""),
                (
                    f"{row.get('location') or ''} {row.get('secondary_locations') or ''}"
                ).casefold(),
                str(row.get("search_blob") or ""),
                str(row.get("category") or ""),
                int(row.get("score") or 0),
                int(row.get("entry_level_score") or 0),
                str(row.get("evidence_confidence") or "low"),
                str(row.get("experience_category") or "experience_unknown"),
                str(row.get("experience_bucket") or "unknown"),
                str(row.get("blocker_status") or "clear"),
                str(row.get("authorization_state") or "clear"),
                int(bool(row.get("active", True))),
                int(bool(row.get("is_software_role", True))),
                int(bool(row.get("remote", False))),
                int(bool(row.get("new_since_previous_run", False))),
                int("nyc" in (row.get("geo_tags") or ())),
                int("new_jersey" in (row.get("geo_tags") or ())),
                int("remote_us" in (row.get("geo_tags") or ())),
                int("other_us" in (row.get("geo_tags") or ())),
                int(row.get("duplicate_count") or 1),
                float(row.get("compensation_value") or 0.0),
                int(row.get("published_epoch") or 0),
                int(row.get("first_seen_epoch") or 0),
                canonical_json(row),
            )
            for row in rows
        ]
        with self._connection(write=True) as connection:
            connection.execute("DELETE FROM canonical_opportunities")
            connection.executemany(
                """
                INSERT INTO canonical_opportunities(
                    job_id, canonical_key, company, company_key, title, title_key,
                    location, location_key, search_blob, match_category, search_score,
                    entry_level_score, evidence_confidence, experience_category,
                    experience_bucket, blocker_status, authorization_state, active,
                    is_software, is_remote, new_since_previous_run, geo_nyc,
                    geo_new_jersey, geo_remote_us, geo_other_us, duplicate_count,
                    compensation_value, published_epoch, first_seen_epoch, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prepared,
            )
        LOGGER.info("Materialized %s canonical opportunities for the dashboard", len(prepared))
        return len(prepared)

    def opportunity_count(self) -> int:
        with self._connection() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM canonical_opportunities").fetchone()[0]
            )

    @staticmethod
    def _opportunity_filters(
        *,
        search: str = "",
        title: str = "",
        company: str = "",
        location: str = "",
        category: str = "",
        min_score: int | None = None,
        confidence: str = "",
        experience: Sequence[str] = (),
        blocker: str = "",
        geo: Sequence[str] = (),
        status: str = "active",
        software: str = "software",
        remote: str = "",
        authorization: str = "",
        new_only: bool = False,
        published_after: int | None = None,
        discovered_after: int | None = None,
    ) -> tuple[list[str], list[Any]]:
        """Translate validated filter values into SQL, always parameterized."""

        clauses: list[str] = []
        parameters: list[Any] = []
        if search:
            clauses.append("search_blob LIKE ?")
            parameters.append(f"%{search.casefold()}%")
        if title:
            clauses.append("title_key LIKE ?")
            parameters.append(f"%{title.casefold()}%")
        if location:
            clauses.append("location_key LIKE ?")
            parameters.append(f"%{location.casefold()}%")
        if company and company != "all":
            clauses.append("company_key = ?")
            parameters.append(company.casefold())
        if category and category != "all":
            clauses.append("match_category = ?")
            parameters.append(category)
        if min_score:
            clauses.append("search_score >= ?")
            parameters.append(int(min_score))
        if confidence and confidence != "all":
            if confidence == "low":
                clauses.append("evidence_confidence = 'low'")
            elif confidence == "medium":
                clauses.append("evidence_confidence IN ('medium', 'high')")
            elif confidence == "high":
                clauses.append("evidence_confidence = 'high'")
        # Experience and geography are multi-select: an empty selection means
        # "do not filter", and several selections are a UNION, not an
        # intersection.  Ticking NYC and Remote US shows jobs matching either.
        buckets = [value for value in experience if value]
        if buckets:
            parts: list[str] = []
            for bucket in buckets:
                if bucket == "blocker":
                    parts.append("blocker_status = 'blocked'")
                else:
                    parts.append("experience_bucket = ?")
                    parameters.append(bucket)
            clauses.append("(" + " OR ".join(parts) + ")")
        tags = [value for value in geo if value]
        if tags:
            columns = {
                "nyc": "geo_nyc",
                "new_jersey": "geo_new_jersey",
                "remote_us": "geo_remote_us",
                "other_us": "geo_other_us",
            }
            parts = [f"{columns[tag]} = 1" for tag in tags if tag in columns]
            if parts:
                clauses.append("(" + " OR ".join(parts) + ")")
        if blocker and blocker != "all":
            clauses.append("blocker_status = ?")
            parameters.append(blocker)
        if authorization and authorization != "all":
            if authorization == "flagged":
                clauses.append("authorization_state <> 'clear'")
            else:
                clauses.append("authorization_state = ?")
                parameters.append(authorization)
        if status == "active":
            clauses.append("active = 1")
        elif status == "closed":
            clauses.append("active = 0")
        if software == "software":
            clauses.append("is_software = 1")
        elif software == "other":
            clauses.append("is_software = 0")
        if remote == "remote":
            clauses.append("is_remote = 1")
        elif remote == "onsite":
            clauses.append("is_remote = 0")
        if new_only:
            clauses.append("new_since_previous_run = 1")
        if published_after:
            clauses.append("published_epoch >= ?")
            parameters.append(int(published_after))
        if discovered_after:
            clauses.append("first_seen_epoch >= ?")
            parameters.append(int(discovered_after))
        return clauses, parameters

    def query_opportunities(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        sort: str = "best",
        **filters: Any,
    ) -> dict[str, Any]:
        """Return one page of opportunities plus the full matching total.

        Filtering happens in SQL over the complete canonical set, so the totals
        and the counts the dashboard displays describe every stored
        opportunity — not whatever subset happened to be rendered.
        """

        order = self.SORT_ORDERS.get(sort, self.SORT_ORDERS["best"])
        size = int(page_size) if int(page_size) in self.PAGE_SIZES else self.DEFAULT_PAGE_SIZE
        clauses, parameters = self._opportunity_filters(**filters)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM canonical_opportunities{where}", parameters
                ).fetchone()[0]
            )
            total_pages = max(1, (total + size - 1) // size)
            current = max(1, min(int(page), total_pages))
            rows = connection.execute(
                f"""
                SELECT id, payload_json FROM canonical_opportunities{where}
                 ORDER BY {order} LIMIT ? OFFSET ?
                """,
                [*parameters, size, (current - 1) * size],
            ).fetchall()
        results = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            # The row id is what the detail endpoint takes, so it travels with
            # the card rather than being reconstructed in the browser.
            payload["id"] = int(row["id"])
            results.append(payload)
        return {
            "total": total,
            "page": current,
            "page_size": size,
            "total_pages": total_pages,
            "returned": len(results),
            "sort": sort if sort in self.SORT_ORDERS else "best",
            "results": results,
        }

    def opportunity_facets(self, **filters: Any) -> dict[str, Any]:
        """Counts and option lists computed over the whole canonical set."""

        clauses, parameters = self._opportunity_filters(**filters)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""

        def grouped(column: str) -> dict[str, int]:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT {column} AS key, COUNT(*) AS total "
                    f"FROM canonical_opportunities{where} GROUP BY key ORDER BY total DESC",
                    parameters,
                ).fetchall()
            return {str(row["key"]): int(row["total"]) for row in rows}

        with self._connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM canonical_opportunities{where}", parameters
                ).fetchone()[0]
            )
            geo = connection.execute(
                f"""
                SELECT SUM(geo_nyc) AS nyc, SUM(geo_new_jersey) AS new_jersey,
                       SUM(geo_remote_us) AS remote_us, SUM(geo_other_us) AS other_us
                  FROM canonical_opportunities{where}
                """,
                parameters,
            ).fetchone()
            companies = connection.execute(
                f"""
                SELECT company, COUNT(*) AS total FROM canonical_opportunities{where}
                 GROUP BY company_key ORDER BY company_key
                """,
                parameters,
            ).fetchall()
        return {
            "total": total,
            "categories": grouped("match_category"),
            "experience": grouped("experience_bucket"),
            "confidence": grouped("evidence_confidence"),
            "blocker": grouped("blocker_status"),
            "authorization": grouped("authorization_state"),
            "geo": {key: int(geo[key] or 0) for key in ("nyc", "new_jersey", "remote_us", "other_us")},
            "companies": [
                {"name": str(row["company"]), "count": int(row["total"])} for row in companies
            ],
        }

    def opportunity_detail(self, opportunity_id: int) -> dict[str, Any] | None:
        """Full card plus the posting description, fetched only on demand.

        Descriptions are never shipped with the list endpoint; a reader who
        expands one card pays for that one card.
        """

        with self._connection() as connection:
            row = connection.execute(
                "SELECT job_id, payload_json FROM canonical_opportunities WHERE id = ?",
                (int(opportunity_id),),
            ).fetchone()
            if row is None:
                return None
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            job = connection.execute(
                "SELECT description_text, description_html FROM jobs WHERE id = ?",
                (int(row["job_id"]),),
            ).fetchone()
        payload["id"] = int(opportunity_id)
        payload["description_text"] = str(job["description_text"] or "") if job else ""
        return payload

    def geo_tag_counts(self, *, active: bool = True, is_software: bool = True) -> dict[str, int]:
        """Count active postings per U.S. geography tag.

        A posting with several tags is counted under each of them, so these
        figures deliberately sum to more than the number of jobs.
        """

        from .geo import GEO_TAGS

        clauses = []
        parameters: list[Any] = []
        if active is not None:
            clauses.append("active = ?")
            parameters.append(int(active))
        if is_software is not None:
            clauses.append("is_software = ?")
            parameters.append(int(is_software))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        counts = {tag: 0 for tag in GEO_TAGS}
        untagged = 0
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT geo_tags_json FROM jobs{where}", parameters
            ).fetchall()
        for row in rows:
            try:
                tags = json.loads(row["geo_tags_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                tags = []
            if not tags:
                untagged += 1
                continue
            for tag in tags:
                if tag in counts:
                    counts[tag] += 1
        counts["no_us_tag"] = untagged
        counts["jobs_considered"] = len(rows)
        return counts

    def experience_category_counts(
        self, *, active: bool = True, is_software: bool = True
    ) -> dict[str, int]:
        """Count active postings per structured experience-evidence category."""

        clauses = []
        parameters: list[Any] = []
        if active is not None:
            clauses.append("active = ?")
            parameters.append(int(active))
        if is_software is not None:
            clauses.append("is_software = ?")
            parameters.append(int(is_software))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT COALESCE(experience_category, 'experience_unknown') AS category,
                       COUNT(*) AS total
                  FROM jobs{where}
                 GROUP BY category ORDER BY total DESC
                """,
                parameters,
            ).fetchall()
        return {str(row["category"]): int(row["total"]) for row in rows}


# Concise compatibility alias for callers that prefer the storage-oriented name.
JobDatabase = Database
