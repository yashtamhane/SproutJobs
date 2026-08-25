"""Canonicalization of source records into distinct displayed opportunities.

This is a *presentation* layer and is deliberately separate from two other things:

* :mod:`jobfinder.db` keeps the source job id as the only identity that matters
  for storage.  Nothing here deletes or merges a stored row.
* :mod:`jobfinder.dedup` groups records for human review and intentionally
  ignores location, so it can surface "the same role posted per city".  That is
  the wrong rule for a dashboard, where two cities are two real choices.

The rule used here is stricter.  Two records describe the same opportunity only
when everything the dashboard shows about them matches — company, title,
location, workplace type, employment type — *and* the substance of the posting
matches too.  A job seeker loses more from a hidden opening than from a repeated
card, so anything ambiguous stays separate.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


# Calibrated against live boards: genuinely repeated postings score at or near
# 1.0, while distinct openings that merely share a template (different business
# unit, extra project section) top out in the low 0.8s.  Staying well above that
# band keeps real openings visible.
DESCRIPTION_SIMILARITY_THRESHOLD = 0.95

# Below this, two short blurbs can overlap by luck, so similarity is not trusted
# and only an exact match counts.
MIN_TOKENS_FOR_SIMILARITY = 25

_STOP_WORDS = frozenset(
    """a an and are as at be by for from in is it of on or our that the this to we will
    with you your""".split()
)


@dataclass(frozen=True)
class CanonicalOpportunity:
    """One displayed opportunity plus every source record it stands for."""

    key: str
    record: Mapping[str, Any]
    records: tuple[Mapping[str, Any], ...]
    reasons: tuple[str, ...]

    @property
    def duplicate_count(self) -> int:
        """How many source records this single displayed opportunity covers."""

        return len(self.records)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(str(_value(item, "source_job_id") or "") for item in self.records)

    @property
    def duplicate_source_ids(self) -> tuple[str, ...]:
        """Source ids that were collapsed into the canonical record."""

        chosen = str(_value(self.record, "source_job_id") or "")
        return tuple(item for item in self.source_ids if item != chosen)

    @property
    def duplicate_job_urls(self) -> tuple[str, ...]:
        chosen = _value(self.record, "job_url")
        urls = []
        for item in self.records:
            url = _value(item, "job_url")
            if url and url != chosen and url not in urls:
                urls.append(str(url))
        return tuple(urls)

    def as_row(self) -> dict[str, Any]:
        """Return the canonical record annotated with its duplicate provenance."""

        row = dict(self.record)
        row["canonical_key"] = self.key
        row["duplicate_count"] = self.duplicate_count
        row["duplicate_source_ids"] = list(self.duplicate_source_ids)
        row["duplicate_job_urls"] = list(self.duplicate_job_urls)
        row["duplicate_reasons"] = list(self.reasons)
        return row


def _value(record: Any, *names: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        for name in names:
            if name in record and record[name] is not None:
                return record[name]
        return default
    for name in names:
        value = getattr(record, name, None)
        if value is not None:
            return value
    return default


def _norm(value: Any) -> str:
    """Lowercase, strip markup, and collapse to comparable word characters."""

    text = re.sub(r"<[^>]+>", " ", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9+#]+", " ", text).strip()


def _norm_title(value: Any) -> str:
    """Normalize a title, dropping requisition numbers that change on reposts."""

    title = _norm(value)
    return re.sub(r"\s+(?:req(?:uisition)?|job)\s*(?:id|#)?\s*\d+\s*$", "", title).strip()


def _canonical_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), re.sub(r"/+$", "", parts.path), "", "")
    )


def _tokens(value: Any) -> set[str]:
    return {word for word in _norm(value).split() if len(word) > 2 and word not in _STOP_WORDS}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _description(record: Any) -> str:
    return str(
        _value(record, "description_text", "description", "description_html", default="") or ""
    )


def display_fields(record: Any) -> tuple[str, ...]:
    """The normalized fields a reader uses to tell two cards apart."""

    company = _value(
        record, "source_company_slug", "company_slug", "company_name", "company", default=""
    )
    return (
        _norm(company),
        _norm_title(_value(record, "title", default="")),
        _norm(_value(record, "location", "primary_location", default="")),
        _norm(_value(record, "workplace_type", "workplace", default="")),
        _norm(_value(record, "employment_type", "employment", default="")),
    )


def canonical_key(record: Any) -> str:
    """Stable key for the bucket of records that could be the same opportunity.

    Matching keys make two records *candidates*; :func:`same_opportunity` decides
    whether they really are.
    """

    return hashlib.sha256("|".join(display_fields(record)).encode("utf-8")).hexdigest()


def same_opportunity(left: Any, right: Any) -> tuple[bool, str]:
    """Decide whether two same-bucket records are one opportunity, and why."""

    left_job = _canonical_url(_value(left, "job_url", "url"))
    right_job = _canonical_url(_value(right, "job_url", "url"))
    if left_job and left_job == right_job:
        return True, "same_job_url"

    left_apply = _canonical_url(_value(left, "apply_url", "application_url"))
    right_apply = _canonical_url(_value(right, "apply_url", "application_url"))
    if left_apply and left_apply == right_apply:
        return True, "same_apply_url"

    # A different pay band is a materially different offer even when every other
    # displayed field agrees.
    left_pay = _norm(_value(left, "compensation_text", default=""))
    right_pay = _norm(_value(right, "compensation_text", default=""))
    if left_pay and right_pay and left_pay != right_pay:
        return False, "different_compensation"

    left_text = _norm(_description(left))
    right_text = _norm(_description(right))
    if left_text and left_text == right_text:
        return True, "same_description"
    if not left_text and not right_text:
        # Nothing distinguishes them: every displayed field already matched.
        return True, "no_distinguishing_detail"
    if not left_text or not right_text:
        return False, "one_description_missing"

    left_tokens, right_tokens = _tokens(left_text), _tokens(right_text)
    if min(len(left_tokens), len(right_tokens)) < MIN_TOKENS_FOR_SIMILARITY:
        return False, "descriptions_too_short_to_compare"
    if _jaccard(left_tokens, right_tokens) >= DESCRIPTION_SIMILARITY_THRESHOLD:
        return True, "near_identical_description"
    return False, "different_description"


def _completeness(record: Any) -> tuple[int, int]:
    """Rank how much a record actually tells the reader."""

    fields = (
        "title",
        "company_name",
        "location",
        "workplace_type",
        "employment_type",
        "department",
        "team",
        "compensation_text",
        "published_at",
        "job_url",
        "apply_url",
    )
    populated = sum(1 for name in fields if str(_value(record, name, default="") or "").strip())
    return populated, len(_description(record))


def _preference(record: Any) -> tuple[Any, ...]:
    """Sort key selecting the canonical record; lower sorts first (better).

    Order follows the documented preference: an active record, then the most
    complete one, then the newest published, then the most recently seen, and
    finally the source id so the outcome never depends on input ordering.
    """

    populated, description_length = _completeness(record)
    return (
        0 if _value(record, "active", default=True) else 1,
        -populated,
        -description_length,
        _descending(str(_value(record, "published_at", default="") or "")),
        _descending(str(_value(record, "first_seen_at", default="") or "")),
        str(_value(record, "source_job_id", default="") or ""),
    )


def _descending(value: str) -> tuple[int, str]:
    """Sort a non-empty ISO timestamp newest-first, with blanks last."""

    return (1, "") if not value else (0, "".join(chr(0x10FFFD - ord(c)) for c in value))


def _components(records: Sequence[Any]) -> list[tuple[list[int], list[str]]]:
    """Group bucket members transitively by :func:`same_opportunity`."""

    adjacency: dict[int, set[int]] = {index: set() for index in range(len(records))}
    reasons: dict[tuple[int, int], str] = {}
    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            matched, reason = same_opportunity(records[left], records[right])
            if matched:
                adjacency[left].add(right)
                adjacency[right].add(left)
                reasons[(left, right)] = reason

    seen: set[int] = set()
    groups: list[tuple[list[int], list[str]]] = []
    for start in range(len(records)):
        if start in seen:
            continue
        stack, component = [start], {start}
        seen.add(start)
        while stack:
            current = stack.pop()
            for neighbor in adjacency[current]:
                if neighbor not in component:
                    component.add(neighbor)
                    seen.add(neighbor)
                    stack.append(neighbor)
        members = sorted(component)
        group_reasons = [
            reason
            for (left, right), reason in reasons.items()
            if left in component and right in component
        ]
        groups.append((members, list(dict.fromkeys(group_reasons))))
    return groups


def canonicalize(records: Iterable[Any]) -> list[CanonicalOpportunity]:
    """Collapse source records into the distinct opportunities worth showing.

    Input order is preserved for the surviving canonical records so callers keep
    whatever ranking they applied beforehand.
    """

    materialized = list(records)
    buckets: dict[str, list[int]] = {}
    for index, record in enumerate(materialized):
        buckets.setdefault(canonical_key(record), []).append(index)

    results: list[tuple[int, CanonicalOpportunity]] = []
    for key, indexes in buckets.items():
        members = [materialized[index] for index in indexes]
        for component, reasons in _components(members):
            group = [members[position] for position in component]
            positions = [indexes[position] for position in component]
            chosen = min(group, key=_preference)
            # Keep the group in the caller's original order for readable output.
            ordered = tuple(
                record for _, record in sorted(zip(positions, group), key=lambda item: item[0])
            )
            results.append(
                (
                    min(positions),
                    CanonicalOpportunity(
                        key=key,
                        record=chosen,
                        records=ordered,
                        reasons=tuple(reasons),
                    ),
                )
            )

    results.sort(key=lambda item: item[0])
    return [opportunity for _, opportunity in results]


def canonical_rows(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Canonicalize and return plain rows annotated with duplicate provenance."""

    return [opportunity.as_row() for opportunity in canonicalize(records)]


__all__ = [
    "DESCRIPTION_SIMILARITY_THRESHOLD",
    "CanonicalOpportunity",
    "canonical_key",
    "canonical_rows",
    "canonicalize",
    "display_fields",
    "same_opportunity",
]
