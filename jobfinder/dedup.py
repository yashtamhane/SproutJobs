"""Conservative semantic duplicate *flagging* without record merging.

Ashby source IDs remain the authoritative identity.  The helpers here only group
records that merit human/pipeline review (for example, a repost under a new ID or
an otherwise identical role published once per location).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


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


def _words(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9+#]+", " ", text).strip()


def _normalized_title(value: Any) -> str:
    title = _words(value)
    # Requisition suffixes change on reposts and convey no semantic distinction.
    title = re.sub(r"\s+(?:req(?:uisition)?|job)\s*(?:id|#)?\s*\d+\s*$", "", title)
    return title.strip()


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
    path = re.sub(r"/+$", "", parts.path)
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, "", ""))


def _description_tokens(value: Any) -> set[str]:
    stop = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "this",
        "to",
        "we",
        "will",
        "with",
        "you",
        "your",
    }
    return {word for word in _words(value).split() if len(word) > 2 and word not in stop}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def semantic_duplicate_key(
    company: str,
    title: str,
    location: str | None = None,
    department: str | None = None,
) -> str:
    """Return a stable candidate-group key that deliberately excludes location.

    Excluding location allows otherwise identical location variants to be flagged.
    The key alone must never be used as a database uniqueness constraint.
    """

    # ``location`` is accepted for API symmetry with normalized-source helpers,
    # but intentionally excluded from candidate grouping.
    del location
    material = "|".join((_words(company), _normalized_title(title), _words(department)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


duplicate_candidate_key = semantic_duplicate_key


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    key: str
    records: tuple[Any, ...]
    confidence: str
    reasons: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.records)

    @property
    def identities(self) -> tuple[tuple[str, str], ...]:
        return tuple(source_identity(record) for record in self.records)

    def as_dict(self) -> dict[str, Any]:
        """Serialize metadata and identities without copying/merging job payloads."""

        return {
            "key": self.key,
            "size": self.size,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "source_identities": [list(identity) for identity in self.identities],
        }


def source_identity(record: Any) -> tuple[str, str]:
    """Return the source identity used to ensure semantic logic never replaces it."""

    source = str(_value(record, "source", default="ashby") or "ashby")
    job_id = str(
        _value(record, "source_job_id", "ashby_job_id", "job_id", "id", default="") or ""
    )
    return source, job_id


def _candidate_key(record: Any) -> str:
    company = _value(record, "source_company_slug", "company_slug", "company_name", "company")
    title = _value(record, "title", default="")
    department = _value(record, "department", "team")
    return semantic_duplicate_key(
        str(company or ""), str(title or ""), department=str(department or "")
    )


def _pair_evidence(left: Any, right: Any) -> tuple[str, ...]:
    evidence: list[str] = []
    left_identity = source_identity(left)
    right_identity = source_identity(right)
    if left_identity[1] and left_identity == right_identity:
        # This is the same source record observed twice, not a semantic duplicate.
        return ()

    left_job_url = _canonical_url(_value(left, "job_url", "url"))
    right_job_url = _canonical_url(_value(right, "job_url", "url"))
    left_apply_url = _canonical_url(_value(left, "apply_url", "application_url"))
    right_apply_url = _canonical_url(_value(right, "apply_url", "application_url"))
    if left_job_url and left_job_url == right_job_url:
        evidence.append("same_canonical_job_url")
    if left_apply_url and left_apply_url == right_apply_url:
        evidence.append("same_canonical_apply_url")

    left_description_value = _value(
        left, "description_text", "description", "description_html", default=""
    )
    right_description_value = _value(
        right, "description_text", "description", "description_html", default=""
    )
    left_normalized_description = _words(left_description_value)
    right_normalized_description = _words(right_description_value)
    left_description = _description_tokens(left_description_value)
    right_description = _description_tokens(right_description_value)
    similarity = _jaccard(left_description, right_description)
    if (
        left_normalized_description == right_normalized_description
        and len(left_normalized_description) >= 80
    ):
        evidence.append("same_description")
    elif similarity >= 0.82 and min(len(left_description), len(right_description)) >= 12:
        evidence.append("highly_similar_description")

    left_location = _words(_value(left, "location"))
    right_location = _words(_value(right, "location"))
    department = _words(_value(left, "department", "team"))
    other_department = _words(_value(right, "department", "team"))
    if (
        left_location
        and right_location
        and left_location != right_location
        and department
        and department == other_department
        and similarity >= 0.65
    ):
        evidence.append("probable_location_variant")
    return tuple(evidence)


def find_duplicate_groups(records: Iterable[Any]) -> tuple[DuplicateGroup, ...]:
    """Find corroborated duplicate candidates while returning original records.

    Grouping requires more than a matching title.  Records must share company,
    normalized title and department, then also share a canonical URL or a highly
    similar substantive description.  Connected components are used so a set of
    location variants appears as one review group.
    """

    buckets: dict[str, list[Any]] = {}
    for record in records:
        company = _value(record, "source_company_slug", "company_slug", "company_name", "company")
        title = _value(record, "title")
        if not company or not title:
            continue
        buckets.setdefault(_candidate_key(record), []).append(record)

    groups: list[DuplicateGroup] = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        adjacency: dict[int, set[int]] = {index: set() for index in range(len(members))}
        pair_reasons: dict[tuple[int, int], tuple[str, ...]] = {}
        for left_index, left in enumerate(members):
            for right_index in range(left_index + 1, len(members)):
                evidence = _pair_evidence(left, members[right_index])
                if evidence:
                    adjacency[left_index].add(right_index)
                    adjacency[right_index].add(left_index)
                    pair_reasons[(left_index, right_index)] = evidence

        remaining = set(range(len(members)))
        while remaining:
            root = remaining.pop()
            stack = [root]
            component = {root}
            while stack:
                current = stack.pop()
                for neighbor in adjacency[current]:
                    if neighbor not in component:
                        component.add(neighbor)
                        remaining.discard(neighbor)
                        stack.append(neighbor)
            if len(component) < 2:
                continue
            reasons: list[str] = []
            for (left_index, right_index), evidence in pair_reasons.items():
                if left_index in component and right_index in component:
                    reasons.extend(evidence)
            unique_reasons = tuple(dict.fromkeys(reasons))
            confidence = (
                "high"
                if {"same_canonical_job_url", "same_canonical_apply_url"} & set(unique_reasons)
                else "medium"
            )
            groups.append(
                DuplicateGroup(
                    key,
                    tuple(members[index] for index in sorted(component)),
                    confidence,
                    unique_reasons,
                )
            )
    return tuple(groups)


group_semantic_duplicates = find_duplicate_groups


__all__ = [
    "DuplicateGroup",
    "duplicate_candidate_key",
    "find_duplicate_groups",
    "group_semantic_duplicates",
    "semantic_duplicate_key",
    "source_identity",
]
