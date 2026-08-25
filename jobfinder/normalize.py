"""Deterministic normalization helpers used by all pipeline stages."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote, urlparse, urlunparse

from .models import NormalizedJob


_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,199})$")
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "dl",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}


def utc_now() -> str:
    """Return a stable, sortable UTC timestamp."""

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_timestamp(value: Any) -> str | None:
    """Convert common source timestamp formats to an ISO-8601 UTC string.

    Unknown string formats are retained instead of being silently discarded.
    """

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if abs(seconds) > 100_000_000_000:  # millisecond Unix timestamps
            seconds /= 1000
        try:
            parsed = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return str(value)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_whitespace(value: Any) -> str:
    """Collapse arbitrary whitespace to a single space."""

    if value is None:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def normalize_plain_text(value: Any) -> str:
    """Tidy supplied plain text without collapsing its line/bullet structure."""

    if value is None:
        return ""
    lines: list[str] = []
    for raw_line in html.unescape(str(value)).replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t\f\v ]+", " ", raw_line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "li":
            self.parts.extend(("\n", "- "))
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if not self._ignored_depth and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def html_to_text(value: Any) -> str:
    """Convert job-description HTML to readable plain text without dependencies."""

    if value is None:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(str(value))
        parser.close()
        raw = "".join(parser.parts)
    except (ValueError, AssertionError):
        raw = re.sub(r"<[^>]*>", " ", str(value))
    lines: list[str] = []
    for raw_line in html.unescape(raw).replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t\f\v ]+", " ", raw_line).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


def extract_ashby_slug(value: str) -> str | None:
    """Extract the first board path component from an Ashby URL or plain slug."""

    candidate = value.strip()
    if not candidate:
        return None
    if candidate.casefold() in {"jobs.ashbyhq.com", "api.ashbyhq.com"}:
        return None
    if _SLUG_RE.fullmatch(candidate):
        return candidate
    parse_target = candidate
    if candidate.lower().startswith("jobs.ashbyhq.com/"):
        parse_target = "https://" + candidate
    parsed = urlparse(parse_target)
    host = (parsed.hostname or "").rstrip(".").lower()
    if host != "jobs.ashbyhq.com" or parsed.scheme.lower() not in {"http", "https"}:
        return None
    segments = [unquote(segment).strip() for segment in parsed.path.split("/") if segment]
    if not segments or not _SLUG_RE.fullmatch(segments[0]):
        return None
    return segments[0]


def ashby_board_url(slug: str) -> str:
    normalized = extract_ashby_slug(slug)
    if normalized is None or normalized != slug.strip():
        raise ValueError(f"Invalid Ashby company slug: {slug!r}")
    return f"https://jobs.ashbyhq.com/{quote(normalized, safe='._~-')}"


def humanize_slug(slug: str) -> str:
    """Derive a readable label from a board slug.

    Ashby's public posting API does not return an organization name, so the slug
    is the only company identifier available.  This is presentation only: the
    verbatim slug is what gets stored as the company's source identity.
    """

    words = [word for word in re.split(r"[.\-_~]+", slug.strip()) if word]
    if not words:
        return slug.strip()
    readable: list[str] = []
    for word in words:
        # Preserve deliberate casing such as "NPX" or "openAI".
        readable.append(word if any(char.isupper() for char in word) else word.capitalize())
    return " ".join(readable)


def canonical_json(value: Any) -> str:
    def encode_unknown(item: Any) -> Any:
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        if isinstance(item, (set, frozenset)):
            return sorted(item, key=str)
        if isinstance(item, datetime):
            return normalize_timestamp(item)
        return str(item)

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=encode_unknown,
    )


def content_hash(value: Any) -> str:
    """Hash a canonical JSON representation so key ordering cannot cause updates."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _named_value(value: Any) -> str | None:
    if isinstance(value, str):
        return normalize_whitespace(value) or None
    if isinstance(value, Mapping):
        for key in ("name", "label", "title", "value"):
            result = normalize_whitespace(value.get(key))
            if result:
                return result
    return None


def _first(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def _source_job_id(raw: Mapping[str, Any], company_slug: str) -> str:
    """Prefer Ashby's ID and safely fall back to its canonical posting URL.

    The public API documentation does not promise an ``id`` field even though
    many live boards provide one.  A URL-derived identity remains stable and never
    falls back to the unsafe company/title combination.
    """

    explicit = normalize_whitespace(_first(raw, "id", "jobId", "jobPostingId"))
    if explicit:
        return explicit
    for key in ("jobUrl", "jobURL", "url", "applyUrl", "applyURL", "applicationUrl"):
        raw_url = normalize_whitespace(raw.get(key))
        if not raw_url:
            continue
        parsed = urlparse(raw_url)
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        if (parsed.hostname or "").rstrip(".").lower() != "jobs.ashbyhq.com":
            continue
        segments = [unquote(item) for item in parsed.path.split("/") if item]
        if segments and segments[-1].casefold() in {"application", "apply"}:
            segments.pop()
        if len(segments) >= 2 and segments[0].casefold() == company_slug.casefold():
            candidate = normalize_whitespace(segments[1])
            if candidate:
                return candidate
        canonical_path = "/" + "/".join(quote(item, safe="._~-") for item in segments)
        canonical_url = urlunparse(
            (parsed.scheme.lower(), parsed.netloc.lower(), canonical_path, "", "", "")
        )
        if canonical_url:
            return "urlsha256:" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    return ""


def _secondary_locations(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return ()
    result: list[str] = []
    for item in value:
        name = _named_value(item)
        if not name and isinstance(item, Mapping):
            name = _named_value(item.get("location"))
        if name and name not in result:
            result.append(name)
    return tuple(result)


def _postal_text(value: Any) -> str:
    """Flatten an Ashby ``address`` block into ``locality, region, country`` text.

    Live boards frequently label a location only as ``Remote`` or by a city name,
    while the structured address states the country.  Preserving that text lets
    the eligibility rules distinguish U.S. from non-U.S. postings reliably.
    """

    if not isinstance(value, Mapping):
        return ""
    postal = value.get("postalAddress")
    if not isinstance(postal, Mapping):
        postal = value
    parts = [
        normalize_whitespace(postal.get(key))
        for key in ("addressLocality", "addressRegion", "addressCountry")
    ]
    return ", ".join(part for part in parts if part)


def _location_context(raw: Mapping[str, Any], secondary: Any) -> str:
    """Collect every structured address on a posting into one searchable string."""

    fragments = [_postal_text(raw.get("address"))]
    if isinstance(secondary, Iterable) and not isinstance(secondary, (str, bytes, Mapping)):
        for item in secondary:
            if isinstance(item, Mapping):
                fragments.append(_postal_text(item.get("address")))
    return "; ".join(dict.fromkeys(fragment for fragment in fragments if fragment))


def _compensation_text(value: Any) -> str | None:
    if isinstance(value, str):
        return normalize_whitespace(value) or None
    if isinstance(value, Mapping):
        for key in (
            "compensationTierSummary",
            "summary",
            "displayText",
            "description",
            "text",
        ):
            text = normalize_whitespace(value.get(key))
            if text:
                return text
        tiers = value.get("tiers")
        if isinstance(tiers, list):
            texts = [_compensation_text(item) for item in tiers]
            joined = "; ".join(text for text in texts if text)
            return joined or None
    if isinstance(value, list):
        texts = [_compensation_text(item) for item in value]
        joined = "; ".join(text for text in texts if text)
        return joined or None
    return None


def semantic_fingerprint(
    company_slug: str,
    title: str,
    location: str | None,
    department: str | None = None,
) -> str:
    """Generate a conservative duplicate candidate key.

    It is intentionally not a database identity: consumers may flag matching keys,
    but should retain each source-level posting.
    """

    # Location is intentionally omitted: Ashby boards commonly publish one source
    # ID per location for the same role.  This key only identifies candidates for
    # review; source rows are never merged automatically.
    tokens = [company_slug, title, department or ""]
    normalized = [re.sub(r"[^a-z0-9]+", " ", item.casefold()).strip() for item in tokens]
    return hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()


def normalize_ashby_job(
    raw: Mapping[str, Any],
    company_slug: str,
    company_name: str | None = None,
) -> NormalizedJob:
    """Normalize one job returned by Ashby's public posting API."""

    source_job_id = _source_job_id(raw, company_slug)
    if not source_job_id:
        raise ValueError("Ashby job is missing a stable id")
    title = normalize_whitespace(raw.get("title"))
    if not title:
        raise ValueError(f"Ashby job {source_job_id!r} is missing a title")

    display_company = normalize_whitespace(company_name) or company_slug
    department = _named_value(_first(raw, "department", "departmentName"))
    team = _named_value(_first(raw, "team", "teamName"))
    location = _named_value(_first(raw, "location", "locationName"))
    raw_secondary = _first(raw, "secondaryLocations", "secondary_locations")
    secondary = _secondary_locations(raw_secondary)
    location_context = _location_context(raw, raw_secondary)
    workplace_type = _named_value(_first(raw, "workplaceType", "workplace_type"))
    employment_type = _named_value(_first(raw, "employmentType", "employment_type"))
    description_html_value = _first(raw, "descriptionHtml", "descriptionHTML", "description")
    description_html = str(description_html_value) if description_html_value is not None else None
    supplied_plain = _first(raw, "descriptionPlain", "descriptionText", "description_plain")
    # Prefer text derived from the HTML: it keeps bullet structure, which the
    # eligibility rules rely on to separate requirement clauses.
    if description_html is not None:
        description_text = html_to_text(description_html)
    elif supplied_plain is not None:
        description_text = normalize_plain_text(supplied_plain)
    else:
        description_text = ""
    if not description_text and supplied_plain is not None:
        description_text = normalize_plain_text(supplied_plain)
    compensation = _first(raw, "compensation", "compensationData", "salary")
    compensation_text = _compensation_text(compensation) or normalize_whitespace(
        raw.get("compensationTierSummary")
    ) or None
    job_url = normalize_whitespace(_first(raw, "jobUrl", "jobURL", "url")) or None
    apply_url = normalize_whitespace(_first(raw, "applyUrl", "applyURL", "applicationUrl")) or None
    published_at = normalize_timestamp(_first(raw, "publishedAt", "published_at", "createdAt"))

    material = {
        "title": title,
        "department": department,
        "team": team,
        "location": location,
        "secondary_locations": secondary,
        "location_context": location_context,
        "workplace_type": workplace_type,
        "employment_type": employment_type,
        "description_html": description_html,
        "description_text": description_text,
        "published_at": published_at,
        "job_url": job_url,
        "apply_url": apply_url,
        "compensation": compensation,
        "compensation_text": compensation_text,
    }
    duplicate_key = semantic_fingerprint(company_slug, title, location, department)
    return NormalizedJob(
        source="ashby",
        source_company_slug=company_slug,
        source_job_id=source_job_id,
        company_name=display_company,
        title=title,
        department=department,
        team=team,
        location=location,
        secondary_locations=secondary,
        location_context=location_context,
        workplace_type=workplace_type,
        employment_type=employment_type,
        description_html=description_html,
        description_text=description_text,
        published_at=published_at,
        job_url=job_url,
        apply_url=apply_url,
        compensation_text=compensation_text,
        compensation=compensation,
        content_hash=content_hash(material),
        semantic_fingerprint=duplicate_key,
        semantic_duplicate_key=duplicate_key,
        raw_data=dict(raw),
    )
