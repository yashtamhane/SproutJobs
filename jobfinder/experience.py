"""Structured experience evidence for new-grad targeting.

This module answers one question and records *why*: does this posting say an
applicant with no professional experience qualifies?

The distinction that matters most here is between two very different reasons a
posting scores low:

* **Negative evidence** — the employer stated a requirement a new graduate does
  not meet ("5+ years of experience required").
* **Unknown** — the employer never said anything about experience at all.

Earlier versions collapsed both into "low score", which quietly buried perfectly
applicable roles next to ones that had explicitly ruled the candidate out.  Here
they are separate categories with separate confidence, so the dashboard can say
"3+ years required" in one case and "not stated" in the other, and so a reader
can filter for exactly the unknown ones and judge them personally.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import html
import re
from typing import Any, Iterable

from .eligibility import ExperienceRequirement


HIGH = "high"
MEDIUM = "medium"
LOW = "low"

# Positive evidence: the posting names the candidate's own situation.
EXPLICIT_NEW_GRAD = "explicit_new_grad"
EXPLICIT_ENTRY_LEVEL = "explicit_entry_level"
RECENT_GRADUATE = "recent_graduate"
EXPLICIT_ZERO_EXPERIENCE = "explicit_zero_experience"
ZERO_TO_ONE_YEAR = "zero_to_one_year"
ZERO_TO_TWO_YEARS = "zero_to_two_years"
# Reachable, but the posting is asking for more than the primary target.
ONE_PLUS_YEAR = "one_plus_year"
ONE_TO_TWO_YEARS = "one_to_two_years"
TWO_PLUS_YEARS = "two_plus_years"
# Negative evidence.
THREE_PLUS_REQUIRED = "three_plus_required"
HARD_BLOCKER = "hard_blocker"
# No evidence either way.
EXPERIENCE_UNKNOWN = "experience_unknown"

#: Every category, ordered from best fit to worst, with ``experience_unknown``
#: deliberately last because it is not a judgement at all.
EXPERIENCE_CATEGORIES: tuple[str, ...] = (
    EXPLICIT_NEW_GRAD,
    EXPLICIT_ZERO_EXPERIENCE,
    ZERO_TO_ONE_YEAR,
    EXPLICIT_ENTRY_LEVEL,
    RECENT_GRADUATE,
    ZERO_TO_TWO_YEARS,
    ONE_PLUS_YEAR,
    ONE_TO_TWO_YEARS,
    TWO_PLUS_YEARS,
    THREE_PLUS_REQUIRED,
    HARD_BLOCKER,
    EXPERIENCE_UNKNOWN,
)

#: Categories that count as the primary new-grad target.
PRIMARY_TARGET: frozenset[str] = frozenset(
    {
        EXPLICIT_NEW_GRAD,
        EXPLICIT_ENTRY_LEVEL,
        RECENT_GRADUATE,
        EXPLICIT_ZERO_EXPERIENCE,
        ZERO_TO_ONE_YEAR,
        ZERO_TO_TWO_YEARS,
    }
)

#: Dashboard filter buckets.  Several categories share a bucket so the filter
#: list stays short enough to be useful.
FILTER_BUCKETS: dict[str, str] = {
    EXPLICIT_NEW_GRAD: "new_grad",
    EXPLICIT_ENTRY_LEVEL: "new_grad",
    RECENT_GRADUATE: "new_grad",
    EXPLICIT_ZERO_EXPERIENCE: "zero_experience",
    ZERO_TO_ONE_YEAR: "zero_to_one",
    ZERO_TO_TWO_YEARS: "zero_to_one",
    ONE_PLUS_YEAR: "one_plus",
    ONE_TO_TWO_YEARS: "one_plus",
    TWO_PLUS_YEARS: "two_plus",
    THREE_PLUS_REQUIRED: "two_plus",
    HARD_BLOCKER: "two_plus",
    EXPERIENCE_UNKNOWN: "unknown",
}

#: Filter bucket -> label, in the order the dashboard shows them.
FILTER_LABELS: tuple[tuple[str, str], ...] = (
    ("new_grad", "Explicit new grad"),
    ("zero_experience", "0 years / no experience"),
    ("zero_to_one", "0–1 years"),
    ("unknown", "Experience unknown"),
    ("one_plus", "1+ years"),
    ("two_plus", "2+ years"),
    ("blocker", "Hard blocker"),
)

CATEGORY_LABELS: dict[str, str] = {
    EXPLICIT_NEW_GRAD: "Explicit new grad",
    EXPLICIT_ENTRY_LEVEL: "Explicit entry level",
    RECENT_GRADUATE: "Recent graduates welcome",
    EXPLICIT_ZERO_EXPERIENCE: "No experience required",
    ZERO_TO_ONE_YEAR: "0–1 years",
    ZERO_TO_TWO_YEARS: "0–2 years",
    ONE_PLUS_YEAR: "1+ years",
    ONE_TO_TWO_YEARS: "1–2 years",
    TWO_PLUS_YEARS: "2+ years",
    THREE_PLUS_REQUIRED: "3+ years required",
    HARD_BLOCKER: "Experience requirement rules this out",
    EXPERIENCE_UNKNOWN: "Not stated",
}


def _clean(value: Any) -> str:
    text = html.unescape("" if value is None else str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


_NEW_GRAD_TITLE = re.compile(
    r"\b(?:new (?:college )?grad(?:uate)?s?|university grad(?:uate)?s?|college graduates?|"
    r"early careers?|grad(?:uate)? (?:software )?(?:engineer|developer)(?:s)?)\b"
)
_ENTRY_TITLE = re.compile(
    r"\bentry[ -]?level\b|"
    r"\b(?:junior|jr\.?)\s+(?:(?:software|backend|front[ -]?end|full[ -]?stack|platform|"
    r"product|data|systems?|embedded)\s+)?(?:engineer|developer)\b|"
    r"\bassociate\s+(?:(?:software|backend|front[ -]?end|full[ -]?stack|platform|product|"
    r"data|systems?)\s+)?(?:engineer|developer)\b|"
    r"\b(?:software\s+)?engineer\s+(?:i|1)\b|\bswe\s+(?:i|1)\b"
)
_RECENT_GRAD_TEXT = re.compile(
    r"\b(?:recent (?:college |university )?graduates?|new graduates?|"
    r"welcom(?:e|ing) (?:applications? from )?(?:recent )?graduates?|"
    r"graduated within (?:the )?(?:last|past) (?:one|two|1|2) years?|"
    r"open to (?:recent )?(?:new )?grad(?:uate)?s?)\b"
)
_ENTRY_TEXT = re.compile(
    r"\bthis is an entry[ -]level (?:role|position|opportunity)\b|"
    r"\bentry[ -]level candidates? (?:are )?(?:welcome|encouraged|considered)\b|"
    r"\bno (?:prior )?industry experience (?:is )?(?:necessary|needed)\b"
)
_NO_EXPERIENCE_REQUIRED = re.compile(
    # Qualifiers stack in real postings ("no prior professional experience"), so
    # allow several rather than exactly one.
    r"\bno\s+(?:(?:prior|previous|professional|work|industry|relevant)\s+){0,3}"
    r"experience\s+(?:is\s+)?(?:required|necessary|needed|expected)\b|"
    r"\b0\s*(?:\+)?\s*years?\s+of\s+experience\b|"
    r"\bzero\s+years?\s+of\s+(?:professional\s+)?experience\b"
)


@dataclass(frozen=True, slots=True)
class ExperienceEvidence:
    """What a posting says about required experience, and how sure we are.

    ``known`` is the field that separates "the employer ruled this out" from
    "the employer never said".  Everything downstream — score, confidence,
    dashboard copy, filters — keys off it.
    """

    category: str
    confidence: str
    delta: int
    summary: str  # short, for the card's Experience row: "3+ years required"
    detail: str  # the reason line: why this leads where it does
    requirement: str = "unstated"  # required / preferred / unstated
    phrase: str | None = None
    known: bool = True
    hard_blocker: bool = False
    # Entry-level wording found in the posting, recorded independently of the
    # verdict above.  A stated number can outrank friendly wording for the
    # *category* without erasing the fact that the wording was there — a
    # posting titled "New Graduate" has said something, and the dashboard must
    # never claim no entry-level evidence exists.
    positive_signal: str | None = None
    positive_phrase: str | None = None
    positive_confidence: str = LOW
    contradiction: bool = False

    @property
    def has_entry_level_wording(self) -> bool:
        return self.positive_signal is not None

    @property
    def label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category.replace("_", " "))

    @property
    def filter_bucket(self) -> str:
        return FILTER_BUCKETS.get(self.category, "unknown")

    @property
    def primary_target(self) -> bool:
        return self.category in PRIMARY_TARGET

    @property
    def negative(self) -> bool:
        return self.delta < 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _band(item: ExperienceRequirement) -> ExperienceEvidence | None:
    """Classify one *required* years-of-experience statement."""

    low, high = item.min_years, item.max_years
    if low >= 5:
        return ExperienceEvidence(
            HARD_BLOCKER,
            HIGH,
            -80,
            f"{low}+ years required",
            "Experience requirement is far beyond the new-grad target.",
            "required",
            item.phrase,
            hard_blocker=True,
        )
    if low >= 4:
        return ExperienceEvidence(
            HARD_BLOCKER,
            HIGH,
            -75,
            f"{low}+ years required",
            "Experience requirement is far beyond the new-grad target.",
            "required",
            item.phrase,
            hard_blocker=True,
        )
    if low >= 3:
        return ExperienceEvidence(
            THREE_PLUS_REQUIRED,
            HIGH,
            -55,
            f"{low}+ years required" if high is None else f"{low}–{high} years required",
            "Experience requirement exceeds the new-grad target.",
            "required",
            item.phrase,
        )
    if low == 0 and high is not None and high <= 1:
        return ExperienceEvidence(
            ZERO_TO_ONE_YEAR,
            HIGH,
            70,
            f"0–{high} years",
            "Stated experience band starts at zero years.",
            "required",
            item.phrase,
        )
    if low == 0 and high is not None and high <= 2:
        return ExperienceEvidence(
            ZERO_TO_TWO_YEARS,
            HIGH,
            64,
            f"0–{high} years",
            "Stated experience band starts at zero years.",
            "required",
            item.phrase,
        )
    if low == 0:
        return ExperienceEvidence(
            EXPLICIT_ZERO_EXPERIENCE,
            HIGH,
            70,
            "0 years",
            "Posting asks for zero years of experience.",
            "required",
            item.phrase,
        )
    if low == 1 and high is not None and high <= 2:
        return ExperienceEvidence(
            ONE_TO_TWO_YEARS,
            MEDIUM,
            30,
            "1–2 years",
            "Just above the new-grad target; internship experience may satisfy it.",
            "required",
            item.phrase,
        )
    if low == 1:
        return ExperienceEvidence(
            ONE_PLUS_YEAR,
            MEDIUM,
            30,
            "1+ years",
            "Slightly above the new-grad target; strong internships may satisfy it.",
            "required",
            item.phrase,
        )
    if low == 2:
        return ExperienceEvidence(
            TWO_PLUS_YEARS,
            MEDIUM,
            -10,
            f"{low}+ years required" if high is None else f"{low}–{high} years required",
            "Generally outside the new-grad target.",
            "required",
            item.phrase,
        )
    return None


def _preferred_band(item: ExperienceRequirement) -> ExperienceEvidence | None:
    """Classify a *preferred* statement, which is a much softer signal.

    "5+ years preferred" is a wish, not a gate, so it costs far less than the
    same figure stated as a requirement.  It is still recorded, because a reader
    deserves to see it.
    """

    low, high = item.min_years, item.max_years
    if low >= 3:
        return ExperienceEvidence(
            THREE_PLUS_REQUIRED,
            MEDIUM,
            -15,
            f"{low}+ years preferred",
            "Preferred experience is above the new-grad target, but it is not required.",
            "preferred",
            item.phrase,
        )
    if low <= 1 and high is not None and high <= 2:
        return ExperienceEvidence(
            ZERO_TO_TWO_YEARS if low == 0 else ONE_TO_TWO_YEARS,
            MEDIUM,
            34 if low == 0 else 26,
            f"{low}–{high} years preferred",
            "Preferred experience band reaches the new-grad target.",
            "preferred",
            item.phrase,
        )
    if low == 2:
        return ExperienceEvidence(
            TWO_PLUS_YEARS,
            LOW,
            -5,
            "2+ years preferred",
            "Preferred experience is above the new-grad target, but it is not required.",
            "preferred",
            item.phrase,
        )
    return None


def _title_wording(title_text: str) -> ExperienceEvidence | None:
    """Positive evidence from the *title*, which is the employer's own label.

    A title is a much stronger statement than a line buried in a description:
    "Software Engineer, New Grad" is who the role is for, full stop.
    """

    match = _NEW_GRAD_TITLE.search(title_text)
    if match:
        return ExperienceEvidence(
            EXPLICIT_NEW_GRAD,
            HIGH,
            70,
            "New grad role",
            "The title says this role is for new graduates.",
            phrase=match.group(0),
        )
    match = _ENTRY_TITLE.search(title_text)
    if match:
        return ExperienceEvidence(
            EXPLICIT_ENTRY_LEVEL,
            HIGH,
            66,
            "Entry-level title",
            f"The title names an entry-level rung ({match.group(0)!r}).",
            phrase=match.group(0),
        )
    return None


def _description_wording(description_text: str) -> ExperienceEvidence | None:
    """Positive evidence from the description body."""

    match = _NEW_GRAD_TITLE.search(description_text) or _RECENT_GRAD_TEXT.search(
        description_text
    )
    if match:
        return ExperienceEvidence(
            RECENT_GRADUATE,
            HIGH,
            62,
            "Recent graduates welcome",
            "The posting says recent graduates are welcome to apply.",
            phrase=match.group(0),
        )
    match = _ENTRY_TEXT.search(description_text)
    if match:
        return ExperienceEvidence(
            EXPLICIT_ENTRY_LEVEL,
            HIGH,
            64,
            "Entry level stated",
            "The posting states this is an entry-level role.",
            phrase=match.group(0),
        )
    return None


def classify_experience(
    title: str | None,
    description: str | None,
    requirements: Iterable[ExperienceRequirement] = (),
) -> ExperienceEvidence:
    """Reduce a posting's experience signals to one explainable verdict.

    Precedence is chosen so the result is never a false positive:

    1. A required four or more years disqualifies regardless of wording.
    2. A required three or more years is strong negative evidence, and beats
       friendly wording, because that number is what a recruiter screens on.
       The conflict is surfaced rather than silently resolved.
    3. An explicit new-grad or entry-level *title* is the employer's own label
       for who the role is for, and outranks any remaining number.
    4. A stated band reaching zero years, or "no experience required", is the
       clearest positive number a posting can give.
    5. Entry-level wording in the description outranks the softer 1+, 1–2 and
       2+ bands, which is the case where friendly wording legitimately changes
       how a number should be read.
    6. Otherwise the number decides.
    7. If nothing said anything, the answer is *unknown* — not *too senior*.
    """

    title_text = _clean(title)
    description_text = _clean(description)
    items = tuple(requirements)
    required = [item for item in items if item.required]
    preferred = [item for item in items if item.preferred]

    strongest_required = max(required, key=lambda value: value.min_years) if required else None
    required_band = _band(strongest_required) if strongest_required else None
    title_signal = _title_wording(title_text)
    body_signal = _description_wording(description_text)
    wording = title_signal or body_signal

    # 1 & 2: a hard number at or above three years wins outright.
    if required_band is not None and required_band.category in {
        HARD_BLOCKER,
        THREE_PLUS_REQUIRED,
    }:
        if wording is not None:
            # The number decides the category, but the wording is kept beside
            # it and the disagreement is stated.  Losing the wording here is
            # what used to make an explicit new-grad posting report that no
            # entry-level evidence existed at all.
            return ExperienceEvidence(
                required_band.category,
                required_band.confidence,
                required_band.delta,
                required_band.summary,
                (
                    f"{required_band.detail} The posting also uses entry-level wording "
                    f"({wording.phrase!r}), so the two disagree — read the requirements."
                ),
                required_band.requirement,
                required_band.phrase,
                hard_blocker=required_band.hard_blocker,
                positive_signal=wording.category,
                positive_phrase=wording.phrase,
                positive_confidence=wording.confidence,
                contradiction=True,
            )
        return required_band

    # 3: the title is the employer's own label; it wins over any number left.
    if title_signal is not None:
        if required_band is not None:
            return ExperienceEvidence(
                title_signal.category,
                title_signal.confidence,
                title_signal.delta,
                title_signal.summary,
                f"{title_signal.detail} Stated experience: {required_band.summary}.",
                required_band.requirement,
                title_signal.phrase,
            )
        return title_signal

    # 4: a band that reaches zero is the strongest positive number there is.
    if required_band is not None and required_band.category in {
        ZERO_TO_ONE_YEAR,
        ZERO_TO_TWO_YEARS,
        EXPLICIT_ZERO_EXPERIENCE,
    }:
        return required_band

    zero_match = _NO_EXPERIENCE_REQUIRED.search(description_text)
    if zero_match and required_band is None:
        return ExperienceEvidence(
            EXPLICIT_ZERO_EXPERIENCE,
            HIGH,
            70,
            "No experience required",
            "The posting states no prior experience is required.",
            phrase=zero_match.group(0),
        )

    # 5: friendly wording in the body outranks the softer numeric bands.
    if body_signal is not None:
        if required_band is not None:
            return ExperienceEvidence(
                body_signal.category,
                body_signal.confidence,
                body_signal.delta,
                body_signal.summary,
                f"{body_signal.detail} Stated experience: {required_band.summary}.",
                required_band.requirement,
                body_signal.phrase,
            )
        return body_signal

    # 6: the number decides.
    if required_band is not None:
        return required_band
    if preferred:
        strongest_preferred = max(preferred, key=lambda value: value.min_years)
        preferred_band = _preferred_band(strongest_preferred)
        if preferred_band is not None:
            return preferred_band

    # 7: silence.  This is *not* a negative verdict, and must never read as one.
    return ExperienceEvidence(
        EXPERIENCE_UNKNOWN,
        LOW,
        0,
        "Not stated",
        "No explicit experience requirement was found in this posting.",
        requirement="unstated",
        known=False,
    )


def bucket_for(category: str) -> str:
    return FILTER_BUCKETS.get(category, "unknown")


def label_for(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " ").capitalize())


__all__ = [
    "CATEGORY_LABELS",
    "EXPERIENCE_CATEGORIES",
    "EXPERIENCE_UNKNOWN",
    "EXPLICIT_ENTRY_LEVEL",
    "EXPLICIT_NEW_GRAD",
    "EXPLICIT_ZERO_EXPERIENCE",
    "FILTER_BUCKETS",
    "FILTER_LABELS",
    "HARD_BLOCKER",
    "ONE_PLUS_YEAR",
    "ONE_TO_TWO_YEARS",
    "PRIMARY_TARGET",
    "RECENT_GRADUATE",
    "THREE_PLUS_REQUIRED",
    "TWO_PLUS_YEARS",
    "ZERO_TO_ONE_YEAR",
    "ZERO_TO_TWO_YEARS",
    "ExperienceEvidence",
    "bucket_for",
    "classify_experience",
    "label_for",
]
