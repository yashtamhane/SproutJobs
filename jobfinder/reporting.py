"""Self-contained HTML and CSV reports for normalized job records.

The reporting boundary deliberately accepts loose row-like objects.  Callers may
pass dictionaries, ``sqlite3.Row`` instances, dataclasses, or simple objects.
Common schema aliases are translated into a small internal representation so the
database and CLI layers do not need to depend on presentation details.

The generated dashboard has no network dependencies.  Filtering and sorting are
performed in the browser, but every piece of source data is rendered and escaped
in Python first.  Only HTTP(S) links are emitted.
"""

from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .experience import (
    EXPERIENCE_CATEGORIES,
    EXPERIENCE_UNKNOWN,
    FILTER_LABELS as EXPERIENCE_FILTERS,
    bucket_for as experience_bucket_for,
    label_for as experience_label,
)
from .geo import GEO_TAGS, label_for as geo_label


DEFAULT_DASHBOARD_PATH = Path("output/index.html")
DEFAULT_CSV_PATH = Path("output/jobs.csv")

CSV_COLUMNS = (
    "score",
    "entry_level_score",
    "category",
    "company",
    "title",
    "department",
    "location",
    "secondary_locations",
    "geo_tags",
    "workplace_type",
    "employment_type",
    "compensation",
    "published_at",
    "first_seen_at",
    "last_seen_at",
    "active",
    "is_software_role",
    "new_since_previous_run",
    "experience_category",
    "experience_summary",
    "experience_detail",
    "blocker_status",
    "authorization_flag",
    "evidence_confidence",
    "uncertain",
    "duplicate_count",
    "duplicate_source_ids",
    "score_reasons",
    "warning_flags",
    "source",
    "source_company_slug",
    "job_url",
    "apply_url",
)

_INFORMATIONAL_FLAGS = {
    "sponsorship_available",
    "graduation_window_compatible",
    "graduation_date_compatible",
    "graduation_year_compatible",
}


@dataclass(frozen=True, slots=True)
class ReportResult:
    """Paths and actual row counts produced by :func:`generate_reports`."""

    dashboard_path: Path
    csv_path: Path
    job_count: int
    active_jobs: int
    software_jobs: int
    strong_matches: int
    new_strong_matches: int

    def as_dict(self) -> dict[str, Any]:
        """Return a serialization-friendly summary for CLI callers."""

        return {
            "dashboard_path": str(self.dashboard_path),
            "csv_path": str(self.csv_path),
            "job_count": self.job_count,
            "active_jobs": self.active_jobs,
            "software_jobs": self.software_jobs,
            "strong_matches": self.strong_matches,
            "new_strong_matches": self.new_strong_matches,
        }


def generate_reports(
    jobs: Iterable[Any],
    dashboard_path: str | os.PathLike[str] = DEFAULT_DASHBOARD_PATH,
    csv_path: str | os.PathLike[str] = DEFAULT_CSV_PATH,
    *,
    generated_at: datetime | None = None,
    title: str = "SproutJobs",
) -> ReportResult:
    """Write the dashboard and CSV from one iterable pass.

    ``jobs`` may be a generator.  It is normalized once so callers get identical
    HTML and CSV even when the input is not re-iterable.
    """

    rows = [_normalize_job(row) for row in jobs]
    dashboard = _write_dashboard_rows(rows, dashboard_path, generated_at, title)
    csv_file = _write_csv_rows(rows, csv_path)
    active_jobs = sum(row["active"] for row in rows)
    software_jobs = sum(
        row["active"] and row["is_software_role"] for row in rows
    )
    strong_matches = sum(
        row["active"]
        and row["is_software_role"]
        and row["category"] == "strong_match"
        for row in rows
    )
    new_strong_matches = sum(
        row["active"]
        and row["is_software_role"]
        and row["category"] == "strong_match"
        and row["new_since_previous_run"]
        for row in rows
    )
    return ReportResult(
        dashboard_path=dashboard,
        csv_path=csv_file,
        job_count=len(rows),
        active_jobs=active_jobs,
        software_jobs=software_jobs,
        strong_matches=strong_matches,
        new_strong_matches=new_strong_matches,
    )


def generate_dashboard(
    jobs: Iterable[Any],
    output_path: str | os.PathLike[str] = DEFAULT_DASHBOARD_PATH,
    *,
    generated_at: datetime | None = None,
    title: str = "SproutJobs",
    totals: Mapping[str, Any] | None = None,
    default_freshness_days: int = 0,
) -> Path:
    """Generate a standalone dashboard and return its resolved output path.

    ``totals`` lets a caller that rendered only part of its data state the real
    figures (``jobs_tracked``, ``active_jobs``, ``software_jobs``,
    ``strong_matches``, ``new_jobs``, ``reportable_jobs``) so the page never
    implies the database holds only what is on screen.

    ``default_freshness_days`` pre-selects a freshness filter; ``0`` shows jobs
    of any age.  The control stays visible either way, so a narrowed view is
    always apparent and one click away from being widened.
    """

    rows = [_normalize_job(row) for row in jobs]
    return _write_dashboard_rows(
        rows, output_path, generated_at, title, totals, default_freshness_days
    )


def write_csv(
    jobs: Iterable[Any],
    output_path: str | os.PathLike[str] = DEFAULT_CSV_PATH,
) -> Path:
    """Generate the spreadsheet-friendly job export."""

    rows = [_normalize_job(row) for row in jobs]
    return _write_csv_rows(rows, output_path)


# Friendly aliases for callers that use symmetrical naming.
generate_csv = write_csv
write_dashboard = generate_dashboard


def _normalize_job(row: Any) -> dict[str, Any]:
    source = _row_mapping(row)
    score = _score(_first(source, "score", "search_score", "match_score"))
    category = _category(
        _first(
            source,
            "category",
            "match_category",
            "eligibility_category",
            default="uncategorized",
        )
    )
    active = _bool(_first(source, "active", "is_active", default=True), True)
    is_software = _bool(
        _first(
            source,
            "is_software_role",
            "software_role",
            "is_software",
            default=True,
        ),
        True,
    )
    is_new = _bool(
        _first(
            source,
            "new_since_previous_run",
            "is_new",
            "new_job",
            default=False,
        ),
        False,
    )

    reasons = _reason_items(
        _first(
            source,
            "score_reasons",
            "score_reasons_json",
            "match_reasons",
            "reasons",
            default=[],
        )
    )
    warnings: list[str] = []
    for warning_key in (
        "warning_flags",
        "eligibility_flags",
        "eligibility_flags_json",
        "warnings",
        "hard_blockers",
    ):
        if warning_key not in source or source[warning_key] is None:
            continue
        for warning in _text_items(source[warning_key]):
            warning_code = re.sub(r"[^a-z0-9]+", "_", warning.lower()).strip("_")
            if warning_code in _INFORMATIONAL_FLAGS:
                continue
            if warning not in warnings:
                warnings.append(warning)
    # Most public postings say nothing about sponsorship or exact requirements, so
    # this is the common case.  It is kept as a quiet note rather than a warning,
    # which reserves the warning styling for genuine eligibility problems.
    uncertain = _bool(_first(source, "uncertain", "eligibility_uncertain", default=False))
    notes = (
        ["Some eligibility details are uncertain; review the full posting."]
        if uncertain
        else []
    )
    authorization_raw = _first(
        source,
        "authorization_flag",
        "work_authorization_flag",
        "sponsorship_flag",
        "authorization_status",
        default="",
    )
    auth_label, auth_state = _authorization(authorization_raw, warnings)

    workplace_type = _text(
        _first(source, "workplace_type", "workplace", "remote_type")
    )
    location = _text(_first(source, "location", "primary_location"))
    remote = _bool(
        _first(source, "is_remote", "remote", default=None),
        default=("remote" in workplace_type.lower() or "remote" in location.lower()),
    )
    compensation, compensation_value = _compensation(source)

    published_raw = _first(
        source, "published_at", "published_date", "posted_at", "created_at"
    )
    first_seen_raw = _first(
        source, "first_seen_at", "discovered_at", "first_discovered_at"
    )
    last_seen_raw = _first(source, "last_seen_at", "updated_at")
    published = _date_value(published_raw)
    first_seen = _date_value(first_seen_raw)
    last_seen = _date_value(last_seen_raw)

    experience_category = _experience_category(
        _first(source, "experience_category", "experience", default="")
    )
    experience_bucket = experience_bucket_for(experience_category)
    blocker_status = _blocker_status(
        _first(source, "blocker_status", default=""), warnings, uncertain
    )
    geo_values = _geo_tags(
        _first(source, "geo_tags", "geo_tags_json", "geography", default=[])
    )

    return {
        "score": score,
        "score_display": _number_display(score),
        "category": category,
        "category_display": _humanize(category),
        "company": _text(
            _first(source, "company", "company_name", "display_name", default="Unknown company")
        )
        or "Unknown company",
        "title": _text(_first(source, "title", "job_title", default="Untitled role"))
        or "Untitled role",
        "department": _text(_first(source, "department", "team")),
        "location": location or "Location not listed",
        "secondary_locations": _joined_text(
            _first(
                source,
                "secondary_locations",
                "secondary_locations_json",
                "additional_locations",
                default=[],
            )
        ),
        "workplace_type": workplace_type,
        "employment_type": _text(
            _first(source, "employment_type", "employment", "job_type")
        ),
        "compensation": compensation,
        "compensation_value": compensation_value,
        "published_at": published["raw"],
        "published_display": published["display"],
        "published_epoch": published["epoch"],
        "first_seen_at": first_seen["raw"],
        "first_seen_display": first_seen["display"],
        "first_seen_epoch": first_seen["epoch"],
        "last_seen_at": last_seen["raw"],
        "active": active,
        "is_software_role": is_software,
        "new_since_previous_run": is_new,
        "remote": remote,
        "authorization_flag": auth_label,
        "authorization_state": auth_state,
        "score_reasons": reasons,
        "warning_flags": warnings,
        "notes": notes,
        "uncertain": uncertain,
        # How many source postings this one displayed opportunity stands for.
        "duplicate_count": max(1, _int(_first(source, "duplicate_count", default=1), 1)),
        "duplicate_source_ids": _text_list(
            _first(source, "duplicate_source_ids", "duplicate_of", default=[])
        ),
        "evidence_confidence": _confidence(
            _first(source, "evidence_confidence", "confidence", default="")
        ),
        "entry_level_score": _int(
            _first(source, "entry_level_score", default=int(score)), int(score)
        ),
        # What the posting said about experience.  ``experience_unknown`` means
        # it never said, which the card must never render as a rejection.
        "experience_category": experience_category,
        "experience_label": experience_label(experience_category),
        "experience_summary": _text(
            _first(source, "experience_summary", default="")
        )
        or ("Not stated" if experience_category == EXPERIENCE_UNKNOWN else ""),
        "experience_detail": _text(_first(source, "experience_detail", default="")),
        "experience_known": experience_category != EXPERIENCE_UNKNOWN,
        "experience_bucket": experience_bucket,
        "blocker_status": blocker_status,
        "geo_tags": geo_values,
        "geo_labels": [geo_label(tag) for tag in geo_values],
        "source": _text(_first(source, "source", default="ashby")),
        "source_company_slug": _text(
            _first(source, "source_company_slug", "company_slug", "slug")
        ),
        "job_url": _safe_url(_first(source, "job_url", "url", "posting_url")),
        "apply_url": _safe_url(
            _first(source, "apply_url", "application_url", "applyUrl")
        ),
    }


def _row_mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if is_dataclass(row) and not isinstance(row, type):
        return asdict(row)
    if hasattr(row, "_asdict"):
        return dict(row._asdict())
    if hasattr(row, "keys"):
        try:
            return {key: row[key] for key in row.keys()}
        except (KeyError, TypeError):
            pass
    try:
        return dict(vars(row))
    except (TypeError, AttributeError) as error:
        raise TypeError(
            "Job rows must be mappings, sqlite-style rows, dataclasses, or objects "
            "with attributes"
        ) from error


def _first(source: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key not in source:
            continue
        value = source[key]
        if value is None:
            continue
        if isinstance(value, (str, bytes, bytearray)) and not _text(value):
            continue
        if isinstance(value, (Mapping, Sequence)) and not isinstance(
            value, (str, bytes, bytearray)
        ) and not value:
            continue
        return value
    return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, str):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            pass
    return str(value).strip()


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    lowered = _text(value).lower()
    if lowered in {"1", "true", "yes", "y", "on", "active", "open"}:
        return True
    if lowered in {"0", "false", "no", "n", "off", "inactive", "closed", ""}:
        return False
    return default


def _experience_category(value: Any) -> str:
    """Normalize a stored experience category, defaulting to *unknown*.

    Defaulting to unknown rather than to a negative band matters: a row written
    before this field existed has genuinely told us nothing, and reading it as
    "too much experience required" would be a fabricated verdict.
    """

    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text if text in EXPERIENCE_CATEGORIES else EXPERIENCE_UNKNOWN


def _blocker_status(value: Any, warnings: list[str], uncertain: bool) -> str:
    text = str(value or "").strip().lower()
    if text in {"clear", "review", "blocked"}:
        return text
    if warnings:
        return "blocked" if any("required" in item.lower() for item in warnings) else "review"
    return "review" if uncertain else "clear"


def _geo_tags(value: Any) -> list[str]:
    """Keep only recognized U.S. geography tags, in display order."""

    values = {
        re.sub(r"[^a-z0-9]+", "_", str(item).strip().lower()).strip("_")
        for item in _text_list(value)
    }
    return [tag for tag in GEO_TAGS if tag in values]


def _confidence(value: Any) -> str:
    """Normalize the evidence-confidence label, defaulting to the cautious one."""

    text = _text(value).strip().lower()
    return text if text in {"high", "medium", "low"} else "low"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _text_list(value: Any) -> list[str]:
    """Flatten a JSON string or sequence into plain strings."""

    return [text for text in (_text(item) for item in _sequence(value)) if text]


def _score(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(result):
        return 0.0
    return max(0.0, min(100.0, result))


def _number_display(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _category(value: Any) -> str:
    category = re.sub(r"[^a-z0-9]+", "_", _text(value).lower()).strip("_")
    aliases = {
        "strong": "strong_match",
        "possible": "possible_match",
        "weak": "weak_match",
        "ineligible": "likely_ineligible",
    }
    return aliases.get(category, category or "uncategorized")


def _humanize(value: str) -> str:
    normalized = value.replace("-", "_").strip("_").lower()
    phrases = {
        "0_1_years": "0–1 Years",
        "0_2_years": "0–2 Years",
        "1_2_years": "1–2 Years",
        "bachelors_cs": "Bachelor's in CS",
        "f1_opt": "F-1 OPT",
        "h1b": "H-1B",
    }
    if normalized in phrases:
        return phrases[normalized]
    acronyms = {
        "ai": "AI",
        "cs": "CS",
        "ml": "ML",
        "nyc": "NYC",
        "opt": "OPT",
        "us": "US",
        "usa": "USA",
    }
    return " ".join(
        acronyms.get(part, part.capitalize()) for part in normalized.split("_") if part
    )


def _jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return []
    if stripped[0:1] not in {"[", "{"}:
        return value
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return value


def _sequence(value: Any) -> list[Any]:
    value = _jsonish(value)
    if value is None or value == "":
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _reason_items(value: Any) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    for item in _sequence(value):
        if is_dataclass(item) and not isinstance(item, type):
            item = asdict(item)
        if isinstance(item, Mapping):
            signal = _text(
                item.get("signal")
                or item.get("code")
                or item.get("reason")
                or item.get("text")
            )
            label = _text(item.get("label") or item.get("message"))
            evidence = _text(item.get("evidence"))
            if not label:
                label = _humanize(signal) if signal else evidence or "Match signal"
            delta = item.get("delta")
            if isinstance(delta, str):
                try:
                    delta = float(delta)
                except ValueError:
                    delta = None
            if not isinstance(delta, (int, float)) or isinstance(delta, bool):
                delta = None
            elif not math.isfinite(float(delta)):
                delta = None
            reasons.append(
                {
                    "signal": signal,
                    "label": label,
                    "delta": delta,
                    "evidence": evidence,
                    "confidence": _confidence(item.get("confidence")),
                }
            )
        else:
            label = _text(item)
            if label:
                reasons.append(
                    {
                        "signal": "",
                        "label": label,
                        "delta": None,
                        "evidence": "",
                        "confidence": "low",
                    }
                )
    return reasons


def _text_items(value: Any) -> list[str]:
    items: list[str] = []
    for item in _sequence(value):
        if is_dataclass(item) and not isinstance(item, type):
            item = asdict(item)
        if isinstance(item, Mapping):
            label = _text(
                item.get("message")
                or item.get("label")
                or item.get("reason")
                or item.get("signal")
                or item.get("code")
            )
            if not label:
                label = ", ".join(
                    f"{_humanize(str(key))}: {_text(val)}" for key, val in item.items()
                )
        else:
            label = _text(item)
        if label:
            items.append(_flag_label(label))
    return items


def _flag_label(value: str) -> str:
    if ("_" in value or "-" in value) and re.fullmatch(
        r"[a-z0-9]+(?:[_-][a-z0-9+]+)+", value
    ):
        return _humanize(value)
    return value


# Which specific restriction a posting states, most severe first.  Keeping
# these apart matters: "you need a clearance" and "we won't sponsor a visa" are
# different problems, and a reader deserves to know which one they hit.
_AUTHORIZATION_LABELS: tuple[tuple[str, str], ...] = (
    ("security_clearance_required", "Security clearance required"),
    ("itar_export_control", "ITAR / export-control restriction"),
    ("citizenship_required", "U.S. citizenship required"),
    ("permanent_resident_only", "Citizens / permanent residents only"),
    ("us_person_required", "U.S.-person status required"),
    ("permanent_work_authorization_required", "Permanent work authorization required"),
    ("opt_not_accepted", "OPT/CPT candidates excluded"),
    ("no_sponsorship", "Explicit sponsorship restriction"),
    ("must_not_require_sponsorship", "Explicit sponsorship restriction"),
)
#: Flags that mean the posting talked about visas without settling the question.
_AUTHORIZATION_UNCERTAIN = (
    "sponsorship_language_present",
    "work_authorization_required_unspecified",
    "export_control_mentioned",
    "security_clearance_mentioned",
    "opt_status_mentioned",
    "public_trust_or_background_check",
)


def _authorization(value: Any, warnings: list[str]) -> tuple[str, str]:
    if isinstance(value, bool):
        label = "Authorization warning" if value else "No explicit restriction found"
    else:
        values = _text_items(value)
        label = "; ".join(values)

    status_code = re.sub(r"[\s-]+", "_", label.lower()).strip("_")
    # The eligibility flags are folded into ``warnings`` upstream, so the exact
    # restriction is available here and the label can name it.
    flag_text = " ".join(
        re.sub(r"[^a-z0-9]+", "_", item.lower()).strip("_") for item in warnings
    )
    specific = next(
        (name for flag, name in _AUTHORIZATION_LABELS if flag in flag_text), ""
    )
    status_labels = {
        # Never claim silence about a posting that discussed visas: the
        # analyzer reports "uncertain" precisely when language was found but no
        # policy was stated, and this label has to say that rather than
        # "not mentioned".
        "unspecified": "No sponsorship information found",
        "positive": "Sponsorship appears available",
        "uncertain": "Sponsorship language present, eligibility uncertain",
        "likely_ineligible": specific or "Explicit authorization restriction found",
        "none": "No explicit restriction found",
        "clear": "No explicit restriction found",
    }
    if status_code == "uncertain" and not any(
        flag in flag_text for flag in _AUTHORIZATION_UNCERTAIN
    ):
        status_labels["uncertain"] = "Authorization details need review"
    label = status_labels.get(status_code, label)

    authorization_text = re.sub(r"[_-]+", " ", label.lower())
    warning_text = re.sub(r"[_-]+", " ", " ".join(warnings).lower())
    lowered = f"{authorization_text} {warning_text}"
    clear_values = {
        "",
        "none",
        "clear",
        "false",
        "no_explicit_restriction",
        "no explicit restriction",
        "no explicit restriction found",
        "no sponsorship information found",
        "sponsorship appears available",
    }
    restriction_terms = (
        "explicit sponsorship restriction",
        "security clearance required",
        "itar",
        "export-control restriction",
        "u.s. citizenship required",
        "citizens / permanent residents only",
        # Hyphens are normalized to spaces before this comparison, so the terms
        # here must be written the way the label looks *after* that.
        "u.s. person status required",
        "permanent work authorization required",
        "opt/cpt candidates excluded",
        "no sponsorship",
        "will not sponsor",
        "without sponsorship",
        "must not require sponsorship",
        "citizen only",
        "citizens only",
        "citizenship required",
        "permanent resident only",
        "permanent residents only",
        "green card only",
        "clearance required",
        "citizenship or permanent residency",
        "authorization restriction",
    )
    uncertainty_terms = (
        "sponsorship language present",
        "uncertain",
        "unknown",
        "unclear",
        "unspecified",
        "not specified",
        "review",
    )
    normalized_clear_values = {item.replace("_", " ") for item in clear_values}
    if authorization_text.strip() in normalized_clear_values:
        if any(term in warning_text for term in restriction_terms):
            return ("Explicit authorization restriction found", "restriction")
        return (label or "No explicit restriction found", "clear")

    if any(term in lowered for term in restriction_terms):
        state = "restriction"
    elif any(term in lowered for term in uncertainty_terms):
        state = "uncertain"
    else:
        state = "warning"
    return (label or "Review authorization details", state)


def _joined_text(value: Any) -> str:
    values = _sequence(value)
    result: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            text = _text(
                item.get("location")
                or item.get("name")
                or item.get("label")
                or item.get("city")
            )
        else:
            text = _text(item)
        if text:
            result.append(text)
    return "; ".join(result)


def _compensation(source: Mapping[str, Any]) -> tuple[str, float]:
    raw = _first(
        source,
        "compensation",
        "compensation_text",
        "compensation_json",
        "salary",
        "salary_range",
        default="",
    )
    explicit_max = _first(
        source,
        "compensation_max",
        "salary_max",
        "max_compensation",
        default=None,
    )
    explicit_min = _first(
        source,
        "compensation_min",
        "salary_min",
        "min_compensation",
        default=None,
    )

    raw = _jsonish(raw)
    if isinstance(raw, Mapping):
        display = _text(
            raw.get("summary")
            or raw.get("compensation_tier_summary")
            or raw.get("compensationTierSummary")
        )
        low = raw.get("min") or raw.get("minimum") or raw.get("minValue")
        high = raw.get("max") or raw.get("maximum") or raw.get("maxValue")
        currency = _text(raw.get("currency") or raw.get("currencyCode") or "USD")
        interval = _text(raw.get("interval") or raw.get("period"))
        if not display and (low is not None or high is not None):
            low_text = _money(low, currency)
            high_text = _money(high, currency)
            display = " – ".join(part for part in (low_text, high_text) if part)
            if interval:
                display += f" / {interval.lower()}"
        raw = display
        explicit_min = explicit_min if explicit_min is not None else low
        explicit_max = explicit_max if explicit_max is not None else high
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        raw = "; ".join(filter(None, (_text(item) for item in raw)))

    display = _text(raw)
    values: list[float] = []
    for candidate in (explicit_max, explicit_min):
        number = _numeric_compensation(candidate)
        if number is not None:
            values.append(number)
    if not values and display:
        values = _numbers_in_compensation(display)
    return display, max(values, default=-1.0)


def _money(value: Any, currency: str) -> str:
    number = _numeric_compensation(value)
    if number is None:
        return ""
    symbol = "$" if currency.upper() in {"USD", "US$", "$", ""} else f"{currency} "
    return f"{symbol}{number:,.0f}"


def _numeric_compensation(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    text = _text(value).replace(",", "").strip()
    match = re.fullmatch(r"[$€£]?\s*(\d+(?:\.\d+)?)\s*([kKmM]?)", text)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return number


def _numbers_in_compensation(value: str) -> list[float]:
    results: list[float] = []
    for match in re.finditer(r"(?<![\w])[$€£]?\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM]?)", value):
        number = float(match.group(1).replace(",", ""))
        suffix = match.group(2).lower()
        if suffix == "k":
            number *= 1_000
        elif suffix == "m":
            number *= 1_000_000
        results.append(number)
    return results


def _safe_url(value: Any) -> str:
    url = _text(value)
    if not url or any(ord(character) < 32 for character in url):
        return ""
    try:
        parsed = urlsplit(url)
        # Accessing port catches malformed bracket/port forms as well.
        _ = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    return url


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = _text(value)
        if not raw:
            return None
        normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
            for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(raw, date_format)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _date_value(value: Any) -> dict[str, Any]:
    raw = _text(value)
    parsed = _parse_datetime(value)
    if parsed is None:
        return {"raw": raw, "display": raw or "Not listed", "epoch": 0}
    return {
        "raw": parsed.isoformat(),
        "display": parsed.strftime("%b %d, %Y").replace(" 0", " "),
        "epoch": int(parsed.timestamp() * 1000),
    }


def _write_dashboard_rows(
    rows: list[dict[str, Any]],
    output_path: str | os.PathLike[str],
    generated_at: datetime | None,
    title: str,
    totals: Mapping[str, Any] | None = None,
    default_freshness_days: int = 0,
) -> Path:
    path = _output_path(output_path)
    rendered = _render_dashboard(rows, generated_at, title, totals, default_freshness_days)
    _atomic_write(path, rendered)
    return path


def _write_csv_rows(
    rows: list[dict[str, Any]], output_path: str | os.PathLike[str]
) -> Path:
    path = _output_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temp_name = output.name
            writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                export = {
                    "score": row["score_display"],
                    "entry_level_score": row["entry_level_score"],
                    "category": row["category"],
                    "company": row["company"],
                    "title": row["title"],
                    "department": row["department"],
                    "location": row["location"],
                    "secondary_locations": row["secondary_locations"],
                    "geo_tags": "; ".join(row["geo_tags"]),
                    "workplace_type": row["workplace_type"],
                    "employment_type": row["employment_type"],
                    "compensation": row["compensation"],
                    "published_at": row["published_at"],
                    "first_seen_at": row["first_seen_at"],
                    "last_seen_at": row["last_seen_at"],
                    "active": str(row["active"]).lower(),
                    "is_software_role": str(row["is_software_role"]).lower(),
                    "new_since_previous_run": str(
                        row["new_since_previous_run"]
                    ).lower(),
                    "experience_category": row["experience_category"],
                    "experience_summary": row["experience_summary"]
                    or row["experience_label"],
                    "experience_detail": row["experience_detail"],
                    "blocker_status": row["blocker_status"],
                    "authorization_flag": row["authorization_flag"],
                    "evidence_confidence": row["evidence_confidence"],
                    "uncertain": str(row["uncertain"]).lower(),
                    "duplicate_count": row["duplicate_count"],
                    "duplicate_source_ids": "; ".join(row["duplicate_source_ids"]),
                    "score_reasons": json.dumps(
                        row["score_reasons"], ensure_ascii=False, separators=(",", ":")
                    ),
                    "warning_flags": json.dumps(
                        row["warning_flags"], ensure_ascii=False, separators=(",", ":")
                    ),
                    "source": row["source"],
                    "source_company_slug": row["source_company_slug"],
                    "job_url": row["job_url"],
                    "apply_url": row["apply_url"],
                }
                writer.writerow(
                    {
                        key: _csv_safe(value) if isinstance(value, str) else value
                        for key, value in export.items()
                    }
                )
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def _output_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.name:
        raise ValueError("An output filename is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _atomic_write(path: Path, contents: str) -> None:
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temp_name = output.name
            output.write(contents)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _csv_safe(value: str) -> str:
    """Prevent values controlled by job boards from becoming sheet formulas."""

    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _e(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _freshness_value(days: Any) -> str:
    """Map a configured day count onto one of the dashboard's filter options."""

    try:
        count = int(days)
    except (TypeError, ValueError):
        return "all"
    if count <= 0:
        return "all"
    for option in (1, 3, 7, 14, 30):
        if count <= option:
            return str(option)
    return "all"


def _count(value: Any, fallback: int) -> int:
    """Use a caller-supplied total when it is a sane number."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    if not math.isfinite(float(value)) or value < 0:
        return fallback
    return max(int(value), fallback)


def _render_dashboard(
    rows: list[dict[str, Any]],
    generated_at: datetime | None,
    title: str,
    totals: Mapping[str, Any] | None = None,
    default_freshness_days: int = 0,
) -> str:
    generated = generated_at or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            not row["active"],
            not row["is_software_role"],
            -row["score"],
            -max(row["published_epoch"], row["first_seen_epoch"]),
            row["company"].casefold(),
        ),
    )
    active_count = sum(row["active"] for row in rows)
    software_count = sum(row["active"] and row["is_software_role"] for row in rows)
    default_count = software_count
    strong_count = sum(
        row["active"]
        and row["is_software_role"]
        and row["category"] == "strong_match"
        for row in rows
    )
    new_count = sum(row["active"] and row["new_since_previous_run"] for row in rows)
    companies = sorted({row["company"] for row in rows}, key=str.casefold)
    categories = sorted({row["category"] for row in rows}, key=str.casefold)

    # When a caller rendered only part of its data, show the real figures and
    # say plainly how much of it reached this page.
    supplied = dict(totals or {})
    tracked_total = _count(supplied.get("jobs_tracked"), len(rows))
    reportable_total = _count(supplied.get("reportable_jobs"), len(rows))
    stats = (
        (_count(supplied.get("jobs_tracked"), len(rows)), "Jobs tracked"),
        (_count(supplied.get("active_jobs"), active_count), "Active"),
        (_count(supplied.get("software_jobs"), software_count), "Active software roles"),
        (_count(supplied.get("strong_matches"), strong_count), "Active strong matches"),
        (_count(supplied.get("new_jobs"), new_count), "New active jobs"),
    )
    stat_cells = "".join(
        f'<div class="stat"><strong>{value:,}</strong><span>{_e(label)}</span></div>\n'
        for value, label in stats
    )
    freshness_options = "".join(
        f'<option value="{_e(value)}"'
        + (" selected" if value == _freshness_value(default_freshness_days) else "")
        + f">{_e(label)}</option>"
        for value, label in (
            ("all", "Any time"),
            ("today", "Today"),
            ("1", "Last 24 hours"),
            ("3", "Last 3 days"),
            ("7", "Last 7 days"),
            ("14", "Last 14 days"),
            ("30", "Last 30 days"),
        )
    )
    scope_note = (
        f"This page lists {len(rows):,} of {reportable_total:,} matching jobs"
        f"{f' from {tracked_total:,} tracked postings' if tracked_total > reportable_total else ''}."
        if reportable_total > len(rows) or tracked_total > len(rows)
        else f"This page lists all {len(rows):,} matching jobs."
    )

    # Multi-select filter groups.  Nothing is ticked by default, and an empty
    # group means "do not filter on this at all" — so the page always opens
    # showing every U.S. region and every experience category, including the
    # unknown ones.  Counts are rendered beside each box so a reader can see
    # what is there before narrowing.
    geo_counts = {
        tag: sum(
            1 for row in rows if row["active"] and row["is_software_role"] and tag in row["geo_tags"]
        )
        for tag in GEO_TAGS
    }
    geo_checkboxes = "".join(
        f'<label class="chip"><input type="checkbox" class="geo-check" value="{_e(tag)}">'
        f'<span>{_e(geo_label(tag))} <small>{geo_counts[tag]:,}</small></span></label>'
        for tag in GEO_TAGS
    )
    bucket_counts: dict[str, int] = {}
    for row in rows:
        if not (row["active"] and row["is_software_role"]):
            continue
        bucket_counts[row["experience_bucket"]] = (
            bucket_counts.get(row["experience_bucket"], 0) + 1
        )
        if row["blocker_status"] == "blocked":
            bucket_counts["blocker"] = bucket_counts.get("blocker", 0) + 1
    experience_checkboxes = "".join(
        f'<label class="chip"><input type="checkbox" class="experience-check" value="{_e(value)}">'
        f'<span>{_e(label)} <small>{bucket_counts.get(value, 0):,}</small></span></label>'
        for value, label in EXPERIENCE_FILTERS
    )

    cards = "".join(_render_card(row, index) for index, row in enumerate(sorted_rows))
    company_options = "".join(
        f'<option value="{_e(company.casefold())}">{_e(company)}</option>'
        for company in companies
    )
    category_options = "".join(
        f'<option value="{_e(category)}">{_e(_humanize(category))}</option>'
        for category in categories
    )
    generated_label = generated.astimezone(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{_e(title)}</title>\n"
        + _STYLES
        + "</head>\n<body>\n"
        '<a class="skip-link" href="#results">Skip to job results</a>\n'
        '<header class="hero">\n<div class="hero-inner">\n'
        '<div class="eyebrow"><span aria-hidden="true">✦</span> LOCAL JOB DISCOVERY</div>\n'
        f"<h1>{_e(title)}</h1>\n"
        '<p class="lede">Ranked opportunities with transparent match signals and direct application links.</p>\n'
        '<div class="summary-grid" aria-label="Report summary">\n'
        + stat_cells
        + "</div>\n"
        f'<p class="generated">Generated {_e(generated_label)} · {_e(scope_note)} · '
        "Data stays on this page.</p>\n"
        "</div>\n</header>\n"
        '<main class="layout">\n'
        '<section class="filters" aria-labelledby="filters-heading">\n'
        '<div class="filter-heading"><div><p class="kicker">Refine</p><h2 id="filters-heading">Find your next role</h2></div>'
        '<button id="resetFilters" class="reset" type="button">Reset filters</button></div>\n'
        '<div class="filter-grid">\n'
        '<label class="field field-wide"><span>Search everything</span><input id="searchInput" type="search" placeholder="Company, title, location, reason…" autocomplete="off"></label>\n'
        '<label class="field"><span>Title keyword</span><input id="titleInput" type="search" placeholder="e.g. backend"></label>\n'
        '<label class="field"><span>Company</span><select id="companyFilter"><option value="all">All companies</option>'
        + company_options
        + "</select></label>\n"
        '<label class="field"><span>Match category</span><select id="categoryFilter"><option value="all">All categories</option>'
        + category_options
        + "</select></label>\n"
        '<label class="field"><span>Status</span><select id="statusFilter"><option value="active" selected>Active only</option><option value="all">Active + closed</option><option value="closed">Closed only</option></select></label>\n'
        '<label class="field"><span>Role type</span><select id="softwareFilter"><option value="software" selected>Software roles</option><option value="all">All roles</option><option value="other">Other roles</option></select></label>\n'
        '<label class="field"><span>Location contains</span><input id="locationInput" type="search" placeholder="NYC, New Jersey, US…"></label>\n'
        '<label class="field"><span>Workplace</span><select id="remoteFilter"><option value="all">Any workplace</option><option value="remote">Remote / distributed</option><option value="onsite">Not marked remote</option></select></label>\n'
        '<label class="field"><span>Evidence confidence</span><select id="confidenceFilter"><option value="all">Any confidence</option><option value="high">High only</option><option value="medium">Medium or better</option><option value="low">Low only</option></select></label>\n'
        '<label class="field"><span>Authorization notes</span><select id="authFilter"><option value="all">Show all</option><option value="clear">No explicit warning</option><option value="flagged">Any warning</option><option value="uncertain">Uncertain / review</option><option value="restriction">Explicit restriction</option></select></label>\n'
        '<label class="field"><span>Freshness source</span><select id="freshnessSource"><option value="published">Date published</option><option value="discovered">First discovered</option></select></label>\n'
        '<label class="field"><span>Freshness</span><select id="freshnessFilter">'
        + freshness_options
        + "</select></label>\n"
        '<label class="field"><span>Minimum score: <output id="scoreOutput" for="scoreFilter">0</output></span><input id="scoreFilter" type="range" min="0" max="100" step="5" value="0"></label>\n'
        '<label class="field"><span>Sort by</span><select id="sortBy"><option value="best">Best match</option><option value="published">Newest published</option><option value="discovered">Newest discovered</option><option value="company">Company A–Z</option><option value="compensation">Highest compensation</option></select></label>\n'
        '<label class="check-field"><input id="newOnly" type="checkbox"><span><strong>New only</strong><small>First found in the latest run</small></span></label>\n'
        "</div>\n"
        '<div class="chip-group" id="geoGroup" role="group" aria-labelledby="geo-legend">\n'
        '<p class="chip-legend" id="geo-legend">Geography <small>tick any combination; none ticked shows every U.S. region</small></p>\n'
        '<div class="chips">'
        + geo_checkboxes
        + "</div>\n</div>\n"
        '<div class="chip-group" id="experienceGroup" role="group" aria-labelledby="experience-legend">\n'
        '<p class="chip-legend" id="experience-legend">Experience evidence <small>&ldquo;Experience unknown&rdquo; means the posting never said — those roles may still fit</small></p>\n'
        '<div class="chips">'
        + experience_checkboxes
        + "</div>\n</div>\n"
        "</section>\n"
        '<section id="results" class="results" aria-labelledby="results-heading">\n'
        f'<div class="results-bar"><div><p class="kicker">Results</p><h2 id="results-heading"><span id="visibleCount">{default_count:,}</span> matching jobs</h2></div><p id="resultStatus" class="result-status" role="status" aria-live="polite"></p></div>\n'
        f'<div id="emptyState" class="empty"{" hidden" if default_count else ""}><span aria-hidden="true">⌁</span><h3>No jobs match these filters</h3><p>Try lowering the score or clearing a location, status, or freshness filter.</p><button id="emptyReset" type="button">Reset filters</button></div>\n'
        f'<div id="jobList" class="job-list">{cards}</div>\n'
        "</section>\n</main>\n"
        '<footer><p>Work-authorization notes are keyword-based screening aids, not legal advice. Always verify the full posting before applying.</p></footer>\n'
        + _SCRIPT
        + "</body>\n</html>\n"
    )


def _render_card(row: dict[str, Any], index: int) -> str:
    searchable = " ".join(
        [
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
                for piece in (reason["label"], reason["evidence"])
                if piece
            ),
            *row["warning_flags"],
        ]
    ).casefold()
    badges = [
        f'<span class="badge category category-{_e(row["category"])}">{_e(row["category_display"])}</span>'
    ]
    if row["new_since_previous_run"]:
        badges.append('<span class="badge new-badge">New</span>')
    if not row["active"]:
        badges.append('<span class="badge closed-badge">Closed</span>')
    if row["remote"]:
        badges.append('<span class="badge remote-badge">Remote</span>')
    if row["duplicate_count"] > 1:
        # The board published this same opening more than once; say so plainly
        # rather than silently dropping the extras.
        badges.append(
            '<span class="badge duplicate-badge" title="'
            f'This employer posted this same opening {row["duplicate_count"]} times">'
            f'{row["duplicate_count"]}× posted</span>'
        )

    for tag, label in zip(row["geo_tags"], row["geo_labels"]):
        badges.append(f'<span class="badge geo-badge geo-{_e(tag)}">{_e(label)}</span>')

    details: list[str] = []
    # Experience comes first: for a new-grad search it is the single most
    # decisive field, and "not stated" is as much an answer as "3+ years".
    experience_css = (
        "experience-unknown" if not row["experience_known"] else "experience-known"
    )
    details.append(
        _detail(
            "Experience",
            row["experience_summary"] or row["experience_label"],
            experience_css,
        )
    )
    if row["workplace_type"]:
        details.append(_detail("Workplace", row["workplace_type"]))
    if row["employment_type"]:
        details.append(_detail("Employment", row["employment_type"]))
    if row["department"]:
        details.append(_detail("Team", row["department"]))
    if row["compensation"]:
        details.append(_detail("Compensation", row["compensation"], "compensation"))
    details.append(_detail("Published", row["published_display"]))
    details.append(_detail("First discovered", row["first_seen_display"]))

    # Lead with the evidence that argues the candidate qualifies, strongest
    # first.  Penalties are still shown, but after the case for the role.
    all_reasons = row["score_reasons"]
    supporting = [item for item in all_reasons if (item["delta"] or 0) > 0]
    against = [item for item in all_reasons if (item["delta"] or 0) < 0]
    neutral = [item for item in all_reasons if not (item["delta"] or 0)]
    supporting.sort(key=lambda item: -float(item["delta"] or 0))
    ordered = [*supporting, *against, *neutral]

    reasons = ""
    if ordered:
        reason_items = []
        for reason in ordered[:6]:
            delta = ""
            css = ""
            if reason["delta"] is not None and reason["delta"]:
                value = float(reason["delta"])
                prefix = "+" if value > 0 else ""
                css = " class=\"against\"" if value < 0 else ""
                delta = f'<span class="delta">{_e(prefix + _number_display(value))}</span>'
            reason_items.append(
                f'<li{css}>{delta}<span>{_e(reason["label"])}</span></li>'
            )
        remainder = len(ordered) - len(reason_items)
        if remainder:
            reason_items.append(f'<li class="more">+{remainder} more signal(s)</li>')
        # A +5 degree mention is not a reason the role "suits" a new graduate, so
        # the confident heading is reserved for evidence that actually argues the
        # case.  Everything else says plainly why the score is what it is.
        real_support = [
            item for item in supporting if (item.get("confidence") or "low") != "low"
        ]
        if real_support:
            heading = "Why this suits an entry-level candidate"
        elif not row["experience_known"]:
            # The honest heading for the unknown case.  "No evidence found"
            # reads as a rejection; this one says the posting was simply quiet.
            heading = "Experience requirement not stated"
        else:
            heading = "Why this scores low"
        confidence = row["evidence_confidence"]
        badge = (
            f'<span class="confidence confidence-{_e(confidence)}">'
            f"{_e(confidence)} confidence</span>"
        )
        explainer = ""
        if not real_support and row["experience_detail"]:
            explainer = f'<p class="evidence-note">{_e(row["experience_detail"])}</p>'
        reasons = (
            f'<div class="evidence"><h4>{_e(heading)} {badge}</h4>'
            + explainer
            + '<ul class="reason-list">'
            + "".join(reason_items)
            + "</ul></div>"
        )

    warnings = list(row["warning_flags"])
    if row["authorization_state"] != "clear" and row["authorization_flag"] not in warnings:
        warnings.insert(0, row["authorization_flag"])
    if warnings:
        warning_html = (
            '<div class="warnings" role="note"><strong>Review before applying</strong><ul>'
            + "".join(f"<li>{_e(warning)}</li>" for warning in warnings[:5])
            + "</ul></div>"
        )
    else:
        # Nothing disqualifying was found.  Say exactly that, plus any soft note.
        note = "; ".join(row["notes"]) if row["notes"] else ""
        warning_html = (
            '<p class="auth-clear"><span aria-hidden="true">✓</span> '
            f'{_e(row["authorization_flag"])}'
            + (f' · {_e(note)}' if note else "")
            + "</p>"
        )

    job_link = (
        f'<a class="button secondary" href="{_e(row["job_url"])}" target="_blank" rel="noopener noreferrer">View Job <span aria-hidden="true">↗</span></a>'
        if row["job_url"]
        else '<span class="button secondary disabled" aria-disabled="true">Job link unavailable</span>'
    )
    apply_link = (
        f'<a class="button apply" href="{_e(row["apply_url"])}" target="_blank" rel="noopener noreferrer">Apply <span aria-hidden="true">→</span></a>'
        if row["apply_url"]
        else '<span class="button apply disabled" aria-disabled="true">Apply link unavailable</span>'
    )
    secondary_location = (
        f'<p class="secondary-location">Also: {_e(row["secondary_locations"])}</p>'
        if row["secondary_locations"]
        else ""
    )
    status_label = "Active" if row["active"] else "Closed"
    initial_visibility = "" if row["active"] and row["is_software_role"] else " hidden"

    return (
        f'<article class="job-card" id="job-{index}"{initial_visibility} '
        f'data-search="{_e(searchable)}" '
        f'data-title="{_e(row["title"].casefold())}" '
        f'data-company="{_e(row["company"].casefold())}" '
        f'data-category="{_e(row["category"])}" '
        f'data-status="{"active" if row["active"] else "closed"}" '
        f'data-software="{str(row["is_software_role"]).lower()}" '
        f'data-new="{str(row["new_since_previous_run"]).lower()}" '
        f'data-remote="{str(row["remote"]).lower()}" '
        f'data-auth="{_e(row["authorization_state"])}" '
        f'data-confidence="{_e(row["evidence_confidence"])}" '
        f'data-experience="{_e(row["experience_category"])}" '
        f'data-experience-bucket="{_e(row["experience_bucket"])}" '
        f'data-blocker="{_e(row["blocker_status"])}" '
        f'data-geo="{_e(" ".join(row["geo_tags"]))}" '
        f'data-location="{_e((row["location"] + " " + row["secondary_locations"]).casefold())}" '
        f'data-score="{row["score"]}" '
        f'data-published="{row["published_epoch"]}" '
        f'data-discovered="{row["first_seen_epoch"]}" '
        f'data-compensation="{row["compensation_value"]}">\n'
        '<div class="card-main">\n'
        '<div class="score" aria-label="Match score '
        + _e(row["score_display"])
        + ' out of 100"><strong>'
        + _e(row["score_display"])
        + '</strong><span>/ 100</span></div>\n'
        '<div class="job-copy"><div class="badges">'
        + "".join(badges)
        + "</div>"
        f'<p class="company">{_e(row["company"])}</p><h3>{_e(row["title"])}</h3>'
        f'<p class="location"><span aria-hidden="true">⌖</span> {_e(row["location"])}</p>{secondary_location}'
        '<span class="sr-only">Status: '
        + status_label
        + "</span></div>\n</div>\n"
        '<dl class="details">'
        + "".join(details)
        + "</dl>"
        + reasons
        + warning_html
        + '<div class="actions">'
        + job_link
        + apply_link
        + "</div>\n</article>\n"
    )


def _detail(label: str, value: str, css_class: str = "") -> str:
    class_attribute = f' class="{_e(css_class)}"' if css_class else ""
    return (
        f"<div{class_attribute}><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>"
    )


_STYLES = r"""<style>
:root {
  --ink: #17231f;
  --muted: #65716b;
  --paper: #f6f7f3;
  --card: #ffffff;
  --line: #dfe5df;
  --green: #176b4d;
  --green-bright: #22a06b;
  --green-soft: #e5f4eb;
  --amber: #9a5b0b;
  --amber-soft: #fff3d6;
  --red: #a53d3d;
  --red-soft: #fde9e7;
  --blue: #315ca8;
  --blue-soft: #e9effb;
  --shadow: 0 14px 34px rgba(20, 42, 33, .08);
  --radius: 18px;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; color: var(--ink); background: var(--paper); line-height: 1.5; }
button, input, select { font: inherit; }
button, select, input[type="checkbox"], input[type="range"] { cursor: pointer; }
a { color: inherit; }
.skip-link { position: fixed; left: 1rem; top: -5rem; z-index: 100; padding: .7rem 1rem; color: white; background: #101b17; border-radius: 8px; }
.skip-link:focus { top: 1rem; }
.hero { color: white; background: radial-gradient(circle at 84% 4%, rgba(67, 185, 128, .27), transparent 30%), linear-gradient(135deg, #10251e, #173f31 65%, #0d2a21); }
.hero-inner { width: min(1180px, calc(100% - 2.5rem)); margin: auto; padding: 4.5rem 0 3.7rem; }
.eyebrow, .kicker { margin: 0 0 .55rem; font-size: .72rem; font-weight: 800; letter-spacing: .13em; text-transform: uppercase; }
.eyebrow { color: #8fe0b6; }
.hero h1 { max-width: 760px; margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: clamp(2.5rem, 6vw, 5.4rem); font-weight: 500; line-height: .98; letter-spacing: -.045em; }
.lede { max-width: 680px; margin: 1.3rem 0 2rem; color: #c7d7d0; font-size: 1.08rem; }
.summary-grid { display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); overflow: hidden; max-width: 930px; border: 1px solid rgba(255,255,255,.15); border-radius: 14px; background: rgba(255,255,255,.06); backdrop-filter: blur(8px); }
.stat { padding: 1rem 1.15rem; border-right: 1px solid rgba(255,255,255,.13); }
.stat:last-child { border-right: 0; }
.stat strong, .stat span { display: block; }
.stat strong { font-size: 1.45rem; font-variant-numeric: tabular-nums; }
.stat span { color: #b9cbc3; font-size: .76rem; }
.generated { margin: 1rem 0 0; color: #9db3aa; font-size: .76rem; }
.layout { width: min(1180px, calc(100% - 2.5rem)); margin: -1.35rem auto 4rem; position: relative; }
.filters { padding: 1.4rem; border: 1px solid var(--line); border-radius: var(--radius); background: var(--card); box-shadow: var(--shadow); }
.filter-heading, .results-bar { display: flex; align-items: end; justify-content: space-between; gap: 1rem; }
.filter-heading h2, .results-bar h2 { margin: 0; font-family: Georgia, "Times New Roman", serif; font-weight: 500; letter-spacing: -.02em; }
.kicker { color: var(--green); }
.reset, .empty button { padding: .55rem .8rem; border: 1px solid var(--line); border-radius: 9px; color: var(--green); background: transparent; font-size: .83rem; font-weight: 700; }
.reset:hover, .empty button:hover { border-color: var(--green); background: var(--green-soft); }
.filter-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: .9rem; margin-top: 1.15rem; }
.field { display: grid; align-content: start; gap: .35rem; min-width: 0; }
.field-wide { grid-column: span 2; }
.field span, .check-field strong { color: #48554f; font-size: .76rem; font-weight: 750; }
.field input:not([type="range"]), .field select { width: 100%; min-height: 42px; padding: .6rem .7rem; border: 1px solid #ced7d1; border-radius: 9px; outline: none; color: var(--ink); background: #fbfcfa; }
.field input:focus, .field select:focus { border-color: var(--green-bright); box-shadow: 0 0 0 3px rgba(34,160,107,.13); }
.field input[type="range"] { width: 100%; accent-color: var(--green); }
.check-field { display: flex; align-items: center; gap: .7rem; align-self: end; min-height: 42px; padding: .45rem .7rem; border: 1px solid #ced7d1; border-radius: 9px; background: #fbfcfa; }
.check-field input { width: 1rem; height: 1rem; accent-color: var(--green); }
.check-field span, .check-field small { display: block; }
.check-field small { color: var(--muted); font-size: .68rem; }
.results { padding-top: 2rem; }
.results-bar { padding: 0 .2rem .8rem; }
.results-bar h2 { font-size: 1.6rem; }
.result-status { margin: 0; color: var(--muted); font-size: .8rem; }
.job-list { display: grid; gap: 1rem; }
.job-card { padding: 1.35rem; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: var(--card); box-shadow: 0 6px 18px rgba(20,42,33,.04); transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
.job-card:hover { transform: translateY(-2px); border-color: #bfd0c5; box-shadow: var(--shadow); }
.job-card[hidden] { display: none; }
.card-main { display: grid; grid-template-columns: 74px 1fr; gap: 1.15rem; }
.score { display: flex; flex-direction: column; align-items: center; justify-content: center; width: 72px; height: 72px; border: 5px solid #b9dfca; border-radius: 50%; color: var(--green); background: var(--green-soft); font-variant-numeric: tabular-nums; }
.score strong { font-size: 1.32rem; line-height: 1; }
.score span { margin-top: .12rem; font-size: .57rem; font-weight: 700; }
.badges { display: flex; flex-wrap: wrap; gap: .4rem; margin-bottom: .55rem; }
.badge { display: inline-flex; padding: .22rem .5rem; border-radius: 99px; font-size: .67rem; font-weight: 800; letter-spacing: .025em; text-transform: uppercase; }
.category { color: var(--green); background: var(--green-soft); }
.category-likely_ineligible { color: var(--red); background: var(--red-soft); }
.category-weak_match { color: var(--amber); background: var(--amber-soft); }
.category-possible_match { color: var(--blue); background: var(--blue-soft); }
.new-badge { color: #fff; background: var(--green); }
.duplicate-badge { color: var(--muted); border: 1px dashed var(--line); background: transparent; }
.closed-badge { color: #626a67; background: #e8ebe9; }
.remote-badge { color: #315ca8; background: var(--blue-soft); }
.geo-badge { color: #4a5a70; border: 1px solid #d3dce8; background: #f2f6fb; }
.geo-nyc, .geo-new_jersey { color: #2f5d8a; border-color: #cadcec; background: #eef5fb; }
.company { margin: 0; color: var(--green); font-size: .82rem; font-weight: 800; }
.job-copy h3 { margin: .1rem 0 .35rem; font-family: Georgia, "Times New Roman", serif; font-size: clamp(1.35rem, 3vw, 1.8rem); font-weight: 500; line-height: 1.18; letter-spacing: -.018em; }
.location, .secondary-location { margin: 0; color: var(--muted); font-size: .86rem; }
.secondary-location { margin-top: .15rem; font-size: .75rem; }
.details { display: grid; grid-template-columns: repeat(auto-fit, minmax(125px, 1fr)); gap: .75rem; margin: 1.2rem 0 0; padding: .9rem 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.details div { min-width: 0; }
.details dt { color: var(--muted); font-size: .66rem; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
.details dd { margin: .18rem 0 0; overflow-wrap: anywhere; font-size: .79rem; font-weight: 650; }
.details .compensation dd { color: var(--green); }
.details .experience-known dd { color: var(--ink); }
.details .experience-unknown dd { color: var(--muted); font-style: italic; font-weight: 600; }
.evidence { margin-top: 1rem; }
.evidence h4 { margin: 0 0 .5rem; font-size: .74rem; text-transform: uppercase; letter-spacing: .055em; }
.evidence-note { margin: 0 0 .5rem; color: var(--muted); font-size: .78rem; line-height: 1.45; }
.chip-group { margin-top: 1.05rem; padding-top: .9rem; border-top: 1px solid var(--line); }
.chip-legend { margin: 0 0 .55rem; color: #48554f; font-size: .76rem; font-weight: 750; }
.chip-legend small { display: block; margin-top: .12rem; color: var(--muted); font-size: .68rem; font-weight: 500; }
.chips { display: flex; flex-wrap: wrap; gap: .45rem; }
.chip { display: inline-flex; align-items: center; gap: .4rem; padding: .38rem .65rem; border: 1px solid #ced7d1; border-radius: 99px; background: #fbfcfa; cursor: pointer; font-size: .75rem; }
.chip input { width: .95rem; height: .95rem; accent-color: var(--green); cursor: pointer; }
.chip small { color: var(--muted); font-weight: 700; }
.chip:has(input:checked) { border-color: var(--green); background: var(--green-soft); }
.chip:has(input:checked) small { color: var(--green); }
.reason-list { display: flex; flex-wrap: wrap; gap: .4rem; margin: 0; padding: 0; list-style: none; }
.reason-list li { display: inline-flex; gap: .35rem; padding: .34rem .55rem; border: 1px solid #d7e7dc; border-radius: 7px; color: #315142; background: #f2f8f4; font-size: .72rem; }
.reason-list .delta { color: var(--green); font-weight: 850; }
.reason-list .more { color: var(--muted); border-style: dashed; background: transparent; }
.reason-list .against { color: #7a4a4a; border-color: #e6cfcf; background: #fcf4f3; }
.reason-list .against .delta { color: var(--red); }
.evidence h4 .confidence { margin-left: .45rem; padding: .1rem .4rem; border-radius: 99px; font-size: .64rem; font-weight: 800; letter-spacing: .02em; text-transform: uppercase; }
.confidence-high { color: var(--green); background: var(--green-soft); }
.confidence-medium { color: var(--blue); background: var(--blue-soft); }
.confidence-low { color: var(--muted); background: #eceeed; }
.warnings { margin-top: .9rem; padding: .75rem .9rem; border-left: 4px solid #d58b25; border-radius: 8px; color: #62431d; background: var(--amber-soft); font-size: .75rem; }
.warnings strong { display: block; }
.warnings ul { margin: .25rem 0 0; padding-left: 1.2rem; }
.auth-clear { margin: .8rem 0 0; color: var(--muted); font-size: .72rem; }
.auth-clear span { color: var(--green); font-weight: 900; }
.actions { display: flex; justify-content: flex-end; gap: .65rem; margin-top: 1rem; }
.button { display: inline-flex; align-items: center; justify-content: center; gap: .35rem; min-height: 42px; padding: .62rem 1rem; border-radius: 9px; font-size: .82rem; font-weight: 800; text-decoration: none; transition: background .15s ease, transform .15s ease; }
.button:hover { transform: translateY(-1px); }
.secondary { border: 1px solid #bdc9c1; color: var(--green); background: transparent; }
.secondary:hover { background: var(--green-soft); }
.apply { min-width: 112px; color: #fff; background: var(--green); box-shadow: 0 6px 14px rgba(23,107,77,.18); }
.apply:hover { background: #11583f; }
.disabled { cursor: not-allowed; opacity: .48; box-shadow: none; }
.empty { padding: 3.5rem 1rem; border: 1px dashed #bdc9c1; border-radius: var(--radius); text-align: center; background: rgba(255,255,255,.45); }
.empty > span { display: block; color: var(--green); font-size: 2rem; }
.empty h3 { margin: .3rem 0; font-family: Georgia, "Times New Roman", serif; font-size: 1.45rem; }
.empty p { margin: 0 auto 1rem; color: var(--muted); }
footer { padding: 1.5rem; color: var(--muted); border-top: 1px solid var(--line); text-align: center; font-size: .74rem; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
@media (max-width: 850px) {
  .summary-grid { grid-template-columns: repeat(3, 1fr); }
  .stat:nth-child(3) { border-right: 0; }
  .stat:nth-child(n+4) { border-top: 1px solid rgba(255,255,255,.13); }
  .filter-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 560px) {
  .hero-inner, .layout { width: min(100% - 1.2rem, 1180px); }
  .hero-inner { padding: 3.2rem 0 2.8rem; }
  .summary-grid { grid-template-columns: repeat(2, 1fr); }
  .stat, .stat:nth-child(3) { border-right: 1px solid rgba(255,255,255,.13); border-top: 1px solid rgba(255,255,255,.13); }
  .stat:nth-child(odd) { border-right: 1px solid rgba(255,255,255,.13); }
  .stat:nth-child(even) { border-right: 0; }
  .filter-grid { grid-template-columns: 1fr; }
  .field-wide { grid-column: span 1; }
  .filter-heading, .results-bar { align-items: flex-start; }
  .result-status { display: none; }
  .job-card { padding: 1rem; }
  .card-main { grid-template-columns: 58px 1fr; gap: .75rem; }
  .score { width: 56px; height: 56px; border-width: 4px; }
  .score strong { font-size: 1rem; }
  .details { grid-template-columns: repeat(2, 1fr); }
  .actions { display: grid; grid-template-columns: 1fr 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
}
@media (prefers-color-scheme: dark) {
  :root { --ink: #e6eee9; --muted: #9eada5; --paper: #101613; --card: #18201c; --line: #303b35; --green: #68c99a; --green-soft: #1d362a; --amber-soft: #3a2b17; --red-soft: #3b2221; --blue-soft: #202e48; --shadow: 0 14px 34px rgba(0,0,0,.22); }
  .field input:not([type="range"]), .field select, .check-field { color: var(--ink); border-color: #3b4841; background: #121a16; }
  .reason-list li { color: #b9ddc9; border-color: #31513f; background: #1a2c23; }
  .chip { color: var(--ink); border-color: #3b4841; background: #121a16; }
  .geo-badge { color: #a8bdd6; border-color: #33425a; background: #1a2331; }
  .geo-nyc, .geo-new_jersey { color: #a8bdd6; border-color: #33425a; background: #1a2331; }
  .warnings { color: #ebc486; }
  .empty { background: rgba(255,255,255,.025); }
}
</style>
"""


_SCRIPT = r"""<script>
(function () {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const controls = {
    search: byId("searchInput"), title: byId("titleInput"), company: byId("companyFilter"),
    category: byId("categoryFilter"), status: byId("statusFilter"), software: byId("softwareFilter"),
    location: byId("locationInput"), remote: byId("remoteFilter"), auth: byId("authFilter"),
    confidence: byId("confidenceFilter"),
    freshnessSource: byId("freshnessSource"), freshness: byId("freshnessFilter"),
    score: byId("scoreFilter"), sort: byId("sortBy"), newOnly: byId("newOnly")
  };
  // Capture whatever the page was rendered with, so "Reset filters" restores the
  // configured starting view rather than a second copy of it hardcoded here.
  const defaults = {};
  Object.entries(controls).forEach(([key, control]) => {
    defaults[key] = control.type === "checkbox" ? control.checked : control.value;
  });
  const list = byId("jobList");
  const cards = Array.from(list.querySelectorAll(".job-card"));
  // Multi-select groups: an empty selection means "do not filter on this",
  // which is what keeps unknown-experience and out-of-state roles visible
  // until the reader deliberately narrows.
  const geoBoxes = Array.from(document.querySelectorAll(".geo-check"));
  const experienceBoxes = Array.from(document.querySelectorAll(".experience-check"));
  function checkedValues(boxes) {
    return boxes.filter((box) => box.checked).map((box) => box.value);
  }
  function matchesGeo(card) {
    const wanted = checkedValues(geoBoxes);
    if (!wanted.length) return true;
    const tags = (card.dataset.geo || "").split(/\s+/).filter(Boolean);
    return wanted.some((tag) => tags.includes(tag));
  }
  function matchesExperience(card) {
    const wanted = checkedValues(experienceBoxes);
    if (!wanted.length) return true;
    const bucket = card.dataset.experienceBucket || "unknown";
    if (wanted.includes(bucket)) return true;
    return wanted.includes("blocker") && card.dataset.blocker === "blocked";
  }

  function normalized(value) { return String(value || "").trim().toLocaleLowerCase(); }
  function truth(value) { return value === "true"; }
  function number(card, key) { const value = Number(card.dataset[key]); return Number.isFinite(value) ? value : 0; }
  function matchesFreshness(card) {
    const range = controls.freshness.value;
    if (range === "all") return true;
    const epoch = number(card, controls.freshnessSource.value);
    if (!epoch) return false;
    const posted = new Date(epoch);
    const now = new Date();
    if (range === "today") {
      return posted.getFullYear() === now.getFullYear() && posted.getMonth() === now.getMonth() && posted.getDate() === now.getDate();
    }
    const age = now.getTime() - epoch;
    return age <= Number(range) * 86400000 && age >= -86400000;
  }
  const confidenceRank = { low: 0, medium: 1, high: 2 };
  function matchesConfidence(card) {
    const want = controls.confidence.value;
    if (want === "all") return true;
    const have = card.dataset.confidence || "low";
    if (want === "low") return have === "low";
    return (confidenceRank[have] ?? 0) >= (confidenceRank[want] ?? 0);
  }
  function matches(card) {
    const search = normalized(controls.search.value);
    const title = normalized(controls.title.value);
    const location = normalized(controls.location.value);
    const status = controls.status.value;
    const software = controls.software.value;
    const remote = controls.remote.value;
    const auth = controls.auth.value;
    return (!search || card.dataset.search.includes(search))
      && (!title || card.dataset.title.includes(title))
      && (controls.company.value === "all" || card.dataset.company === controls.company.value)
      && (controls.category.value === "all" || card.dataset.category === controls.category.value)
      && (status === "all" || card.dataset.status === status)
      && (software === "all" || (software === "software") === truth(card.dataset.software))
      && (!location || card.dataset.location.includes(location))
      && (remote === "all" || (remote === "remote") === truth(card.dataset.remote))
      && (auth === "all" || (auth === "flagged" ? card.dataset.auth !== "clear" : card.dataset.auth === auth))
      && matchesConfidence(card)
      && matchesGeo(card)
      && matchesExperience(card)
      && number(card, "score") >= Number(controls.score.value)
      && (!controls.newOnly.checked || truth(card.dataset.new))
      && matchesFreshness(card);
  }
  function compare(a, b) {
    const sort = controls.sort.value;
    if (sort === "company") return a.dataset.company.localeCompare(b.dataset.company) || number(b, "score") - number(a, "score");
    if (sort === "published") return number(b, "published") - number(a, "published") || number(b, "score") - number(a, "score");
    if (sort === "discovered") return number(b, "discovered") - number(a, "discovered") || number(b, "score") - number(a, "score");
    if (sort === "compensation") return number(b, "compensation") - number(a, "compensation") || number(b, "score") - number(a, "score");
    return number(b, "score") - number(a, "score")
      || Math.max(number(b, "published"), number(b, "discovered")) - Math.max(number(a, "published"), number(a, "discovered"));
  }
  function applyFilters() {
    const visible = cards.filter(matches).sort(compare);
    const visibleSet = new Set(visible);
    visible.forEach((card) => { card.hidden = false; list.appendChild(card); });
    cards.forEach((card) => { if (!visibleSet.has(card)) card.hidden = true; });
    byId("visibleCount").textContent = visible.length.toLocaleString();
    byId("scoreOutput").value = controls.score.value;
    byId("emptyState").hidden = visible.length !== 0;
    byId("resultStatus").textContent = visible.length === cards.length
      ? `Showing all ${cards.length.toLocaleString()} jobs on this page`
      : `Showing ${visible.length.toLocaleString()} of ${cards.length.toLocaleString()} on this page`;
  }
  function reset() {
    Object.entries(defaults).forEach(([key, value]) => {
      if (typeof value === "boolean") controls[key].checked = value;
      else controls[key].value = value;
    });
    geoBoxes.concat(experienceBoxes).forEach((box) => { box.checked = false; });
    applyFilters();
  }
  Object.values(controls).forEach((control) => {
    control.addEventListener(control.matches('input[type="search"], input[type="range"]') ? "input" : "change", applyFilters);
  });
  geoBoxes.concat(experienceBoxes).forEach((box) => {
    box.addEventListener("change", applyFilters);
  });
  byId("resetFilters").addEventListener("click", reset);
  byId("emptyReset").addEventListener("click", reset);
  applyFilters();
})();
</script>
"""


__all__ = [
    "CSV_COLUMNS",
    "DEFAULT_CSV_PATH",
    "DEFAULT_DASHBOARD_PATH",
    "ReportResult",
    "generate_csv",
    "generate_dashboard",
    "generate_reports",
    "write_csv",
    "write_dashboard",
]
