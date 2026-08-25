"""Shared data models for source adapters and persistence.

The models in this module deliberately contain no network or database logic.  This
keeps source adapters replaceable (Greenhouse/Lever can implement the same result
types later) and makes normalized jobs straightforward to fixture in tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CompanyCandidate:
    """A possible public job board found by a discovery provider."""

    slug: str
    discovery_source: str
    discovery_evidence: str | None = None
    candidate_url: str | None = None
    source: str = "ashby"


@dataclass(slots=True)
class NormalizedJob:
    """Source-independent representation of a public job posting."""

    source: str
    source_company_slug: str
    source_job_id: str
    company_name: str
    title: str
    department: str | None = None
    team: str | None = None
    location: str | None = None
    secondary_locations: tuple[str, ...] = ()
    # Structured address text (city/region/country) kept separate from the
    # display locations so eligibility rules can see the country reliably.
    location_context: str = ""
    workplace_type: str | None = None
    employment_type: str | None = None
    description_html: str | None = None
    description_text: str = ""
    published_at: str | None = None
    job_url: str | None = None
    apply_url: str | None = None
    compensation_text: str | None = None
    compensation: Mapping[str, Any] | list[Any] | str | None = None
    content_hash: str = ""
    semantic_fingerprint: str = ""
    raw_data: Mapping[str, Any] = field(default_factory=dict)
    # Analysis is attached after source normalization and before persistence.
    is_software: bool | None = None
    search_score: int | None = None
    eligibility_category: str | None = None
    match_category: str | None = None
    eligibility_flags: tuple[str, ...] = ()
    score_reasons: tuple[Mapping[str, Any], ...] = ()
    authorization_flag: str | None = None
    uncertain: bool = False
    # How strong the evidence is that an entry-level candidate qualifies.
    evidence_confidence: str = "low"
    # Entry-level fit before any eligibility blocker caps the ranking score.
    entry_level_score: int | None = None
    # What the posting said about required experience.  ``experience_unknown``
    # means the posting was silent, which is deliberately not the same thing as
    # asking for too much.
    experience_category: str = "experience_unknown"
    experience_summary: str = "Not stated"
    experience_detail: str = ""
    # clear / review / blocked, independent of entry-level fit.
    blocker_status: str = "clear"
    # U.S. geography buckets that apply; a posting may carry several.
    geo_tags: tuple[str, ...] = ()
    semantic_duplicate_key: str | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable source snapshot."""

        value = asdict(self)
        value["secondary_locations"] = list(self.secondary_locations)
        value["geo_tags"] = list(self.geo_tags)
        return value


@dataclass(frozen=True, slots=True)
class BoardValidation:
    """Result of checking whether a candidate slug is a real public board."""

    slug: str
    valid: bool
    display_name: str | None = None
    job_count: int | None = None
    board_url: str | None = None
    error: str | None = None
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class BoardFetchResult:
    """A successfully fetched and normalized board."""

    source: str
    slug: str
    display_name: str
    board_url: str
    fetched_at: str
    jobs: tuple[NormalizedJob, ...]
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngestionStats:
    """Changes made while atomically ingesting one successful board fetch."""

    company_id: int
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    reopened: int = 0
    unchanged: int = 0
    closed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: int
    started_at: str
    completed_at: str | None
    status: str
    command: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    stats: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
