"""Deterministic software-role classification.

The classifier intentionally uses explainable text rules rather than a hosted
model.  It answers only whether a posting is materially a software-engineering
opportunity; entry-level suitability is handled separately by :mod:`scoring`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import html
import re
from typing import Any


def _text(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip().lower()


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    """One auditable rule that contributed to a classification."""

    signal: str
    weight: int
    source: str
    phrase: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SoftwareClassification:
    """Result returned by :func:`classify_software_role`.

    ``score`` is a classifier confidence score, not the candidate-match score.
    It is kept in the 0--100 range solely to make borderline decisions visible.
    """

    is_software_role: bool
    score: int
    confidence: str
    uncertain: bool
    reasons: tuple[ClassificationEvidence, ...]

    @property
    def is_software(self) -> bool:
        """Compatibility/readability alias used by report code."""

        return self.is_software_role

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_software_role": self.is_software_role,
            "score": self.score,
            "confidence": self.confidence,
            "uncertain": self.uncertain,
            "reasons": [reason.as_dict() for reason in self.reasons],
        }


_STRONG_TITLE_PATTERNS: tuple[tuple[str, str, int], ...] = (
    ("software_engineer_title", r"\bsoftware\s+(?:development\s+)?engineer(?:ing)?\b", 62),
    ("software_developer_title", r"\bsoftware\s+developer\b", 62),
    (
        "backend_title",
        r"\b(?:back[ -]?end|server[ -]?side)\s+(?:software\s+)?(?:engineer|developer)\b",
        58,
    ),
    (
        "full_stack_title",
        r"\b(?:full[ -]?stack|fullstack)\s+(?:software\s+)?(?:engineer|developer)\b",
        58,
    ),
    (
        "frontend_mobile_title",
        r"\b(?:front[ -]?end|frontend|mobile|ios|android|web)\s+"
        r"(?:software\s+)?(?:engineer|developer)\b",
        56,
    ),
    ("swe_title", r"\bswe(?:\s+(?:i|ii|iii|iv|1|2|3|4))?\b", 58),
    ("firmware_title", r"\bfirmware\s+(?:engineer|developer)\b", 54),
    (
        "target_engineer_title",
        r"\b(?:platform|infrastructure|product)\s+(?:software\s+)?engineer\b",
        53,
    ),
    ("developer_title", r"\b(?:application\s+)?developer\b", 52),
    ("site_reliability_title", r"\b(?:site reliability|devops)\s+engineer\b|\bsre\b", 52),
)

_CONTEXT_TITLE_PATTERNS: tuple[tuple[str, str, int], ...] = (
    ("application_engineer_title", r"\bapplication\s+engineer\b", 46),
    ("systems_engineer_title", r"\bsystems?\s+engineer\b", 25),
    (
        "ml_ai_engineer_title",
        r"\b(?:machine learning|ml|artificial intelligence|ai)\s+(?:software\s+)?engineer\b",
        35,
    ),
    ("data_engineer_title", r"\bdata\s+(?:platform\s+)?engineer\b", 32),
    ("security_engineer_title", r"\b(?:application|product|cloud)?\s*security engineer\b", 25),
)

_CLEAR_NON_SOFTWARE_TITLES: tuple[tuple[str, str], ...] = (
    ("engineering_manager_title", r"\b(?:software\s+)?engineering manager\b"),
    ("product_manager_title", r"\bproduct manager\b"),
    ("program_manager_title", r"\btechnical program manager\b"),
    (
        "physical_engineering_title",
        r"\b(?:mechanical|civil|chemical|structural|manufacturing|industrial|"
        r"electrical|hardware) engineer\b",
    ),
    ("analyst_title", r"\b(?:data|business|financial) analyst\b"),
    ("it_support_title", r"\b(?:help\s*desk|desktop support|it support)\b"),
    ("recruiting_title", r"\b(?:recruiter|talent acquisition)\b"),
    ("customer_success_title", r"\bcustomer success\b"),
    ("developer_relations_title", r"\b(?:developer advocate|developer relations|devrel)\b"),
)

_COMMERCIAL_TITLE = re.compile(
    r"\b(?:sales engineer|solutions? engineer|solutions? consultant|customer engineer|"
    r"technical account manager)\b"
)
_COMMERCIAL_CONTEXT = re.compile(
    r"\b(?:pre[ -]?sales|sales cycle|quota|close deals?|prospects?|demos?|"
    r"customer adoption|account executive|revenue team|rfps?|proofs? of concept)\b"
)
_SOFTWARE_CONTEXT_PATTERNS: tuple[tuple[str, str, int], ...] = (
    (
        "software_delivery_description",
        r"\b(?:build|develop|design|implement|maintain|ship|test)\w*\s+(?:[\w -]+\s+){0,3}"
        r"(?:software|applications?|services?|apis?|platform|systems?)\b",
        22,
    ),
    (
        "programming_description",
        r"\b(?:programming|coding|codebase|source code|data structures|algorithms?)\b",
        15,
    ),
    (
        "software_technology_description",
        r"\b(?:python|java(?:script)?|typescript|c\+\+|c#|golang|\bgo\b|rust|ruby|scala|"
        r"react|node\.js|sql|kubernetes|distributed systems?|microservices?|rest(?:ful)? apis?)\b",
        13,
    ),
)
_NON_SOFTWARE_CONTEXT = re.compile(
    r"\b(?:cold call|book of business|sales targets?|quota|commission|technical support tickets?|"
    r"repair hardware|circuit design|cad drawings?|construction documents?)\b"
)
# A department that plainly builds software lends credibility to an unusual
# title such as "Member of Technical Staff" or "Model Behavior Engineer".
_ENGINEERING_DEPARTMENT = re.compile(r"\b(?:engineering|software|technology|r ?& ?d)\b")
# Ceilings applied when no title pattern matched.  Description keywords such as
# "python" or "build systems" appear in finance, data, and sales postings too,
# so on their own they must not be able to cross the classification threshold.
_CONTEXT_ONLY_CEILING = 40
_ENGINEERING_CONTEXT_ONLY_CEILING = 60


def classify_software_role(
    title: str | None,
    description: str | None = None,
    department: str | None = None,
) -> SoftwareClassification:
    """Classify a posting using its title and substantive description.

    Strong source-specific identities, seniority, and eligibility are deliberately
    not considered here.  For example, ``Senior Software Engineer`` is still a
    software role even though the candidate-match scorer will reject its seniority.
    """

    title_text = _text(title)
    description_text = _text(description)
    department_text = _text(department)
    combined_context = f"{department_text} {description_text}".strip()
    evidence: list[ClassificationEvidence] = []
    score = 0

    clear_non_software = False
    for signal, pattern in _CLEAR_NON_SOFTWARE_TITLES:
        match = re.search(pattern, title_text)
        if match:
            clear_non_software = True
            evidence.append(ClassificationEvidence(signal, -100, "title", match.group(0)))
            break

    # Only the strongest synonymous title rule contributes, preventing titles such
    # as "Backend Software Engineer" from receiving duplicate evidence.
    title_matches: list[tuple[int, str, str]] = []
    for signal, pattern, weight in (*_STRONG_TITLE_PATTERNS, *_CONTEXT_TITLE_PATTERNS):
        match = re.search(pattern, title_text)
        if match:
            title_matches.append((weight, signal, match.group(0)))
    selected_title_signal: str | None = None
    if title_matches:
        weight, signal, phrase = max(title_matches)
        selected_title_signal = signal
        score += weight
        evidence.append(ClassificationEvidence(signal, weight, "title", phrase))

    commercial_match = _COMMERCIAL_TITLE.search(title_text)
    commercial_context = bool(_COMMERCIAL_CONTEXT.search(combined_context))
    if commercial_match:
        penalty = -65 if commercial_context else -25
        score += penalty
        evidence.append(
            ClassificationEvidence(
                "commercial_engineering_role", penalty, "title", commercial_match.group(0)
            )
        )

    for signal, pattern, weight in _SOFTWARE_CONTEXT_PATTERNS:
        match = re.search(pattern, combined_context)
        if match:
            score += weight
            evidence.append(ClassificationEvidence(signal, weight, "description", match.group(0)))

    non_software_context = _NON_SOFTWARE_CONTEXT.search(combined_context)
    if non_software_context:
        score -= 35
        evidence.append(
            ClassificationEvidence(
                "non_software_duties", -35, "description", non_software_context.group(0)
            )
        )

    if not title_matches:
        engineering_department = bool(_ENGINEERING_DEPARTMENT.search(department_text))
        ceiling = (
            _ENGINEERING_CONTEXT_ONLY_CEILING
            if engineering_department
            else _CONTEXT_ONLY_CEILING
        )
        if score > ceiling:
            evidence.append(
                ClassificationEvidence(
                    "title_evidence_missing", ceiling - score, "title", title_text or None
                )
            )
            score = ceiling
    if clear_non_software:
        score = min(score, 10)
    if commercial_match and commercial_context:
        score = min(score, 20)

    score = max(0, min(100, score))
    is_software = score >= 45
    # Scores near the decision boundary or context-dependent engineer titles are
    # surfaced instead of being represented as false certainty.
    context_signals = {item[0] for item in _CONTEXT_TITLE_PATTERNS}
    strong_signals = {item[0] for item in _STRONG_TITLE_PATTERNS}
    context_only = selected_title_signal in context_signals
    strong_title = selected_title_signal in strong_signals
    uncertain = not clear_non_software and (
        (not strong_title and 30 <= score < 60) or (context_only and score < 70)
    )
    confidence = "high" if score >= 70 or score <= 15 else "medium" if not uncertain else "low"
    return SoftwareClassification(is_software, score, confidence, uncertain, tuple(evidence))


def is_software_role(
    title: str | None,
    description: str | None = None,
    department: str | None = None,
) -> bool:
    """Boolean convenience wrapper around :func:`classify_software_role`."""

    return classify_software_role(title, description, department).is_software_role


# Concise alias for callers that already operate on job fields.
classify_job = classify_software_role


__all__ = [
    "ClassificationEvidence",
    "SoftwareClassification",
    "classify_job",
    "classify_software_role",
    "is_software_role",
]
