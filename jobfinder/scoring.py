"""Transparent entry-level suitability scoring for normalized job fields.

The score answers one question: *is there evidence a 2026 graduate qualifies?*

That is deliberately not the same question as "is this a software job".  Software
classification is only a gate — it decides whether a posting is considered at all
and contributes nothing to the score.  Earlier versions gave every software role
a large base score, which meant several hundred ordinary mid-level postings
surfaced as plausible matches on no evidence whatsoever.  Here a posting starts
at zero and has to earn its way up with something a reader can point at.

Four separate concepts come out of this module, and keeping them apart is what
makes a low score readable:

``entry_level_score``
    0–100 fit for a new graduate, from evidence alone.
``evidence_confidence``
    How good that evidence is — ``high``, ``medium`` or ``low``.
``experience_evidence_category``
    What the posting actually said about experience, including the crucial
    ``experience_unknown``, which is *not* a negative verdict.
``blocker_status``
    Whether something disqualifies the candidate outright, independent of fit.

A posting can be a perfect entry-level fit and still be blocked (a citizenship
requirement), or have no blockers at all and simply never mention experience.
Those are different situations and the dashboard says which is which.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import html
import re
from typing import Any, Iterable, Mapping

from .classify import SoftwareClassification, classify_software_role
from .eligibility import EligibilityAnalysis, analyze_eligibility
from .experience import EXPERIENCE_UNKNOWN, ExperienceEvidence, classify_experience
from .geo import GeoAnalysis, analyze_geography, geography_from_job


def _clean(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip().lower()


# Category thresholds.  A single piece of high-confidence evidence ("New Grad",
# "0-2 years") is enough to reach a strong match on its own; medium evidence
# ("2+ years", an all-levels ladder that reaches entry) reaches possible; a
# posting with no entry-level evidence stays weak no matter how good a software
# job it is.
STRONG_MATCH_SCORE = 60
POSSIBLE_MATCH_SCORE = 30
# A posting with a hard blocker cannot score above this, so disqualified roles
# never outrank genuine matches in a score-ordered view.
BLOCKED_SCORE_CEILING = 20

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
_CONFIDENCE_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}


@dataclass(frozen=True, slots=True)
class ScoreReason:
    """A machine-readable and human-displayable score adjustment."""

    signal: str
    delta: int
    evidence: str | None = None
    confidence: str = LOW

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: int
    category: str
    reasons: tuple[ScoreReason, ...]
    warnings: tuple[str, ...]
    uncertain: bool
    is_software_role: bool
    hard_blockers: tuple[str, ...]
    evidence_confidence: str = LOW
    # Entry-level fit before any blocker cap is applied.  Kept separate so a
    # blocked posting can still say "this would otherwise have been a strong
    # match", which is exactly what a reader wants to know.
    entry_level_score: int = 0
    experience_category: str = EXPERIENCE_UNKNOWN
    experience_summary: str = "Not stated"
    experience_detail: str = "No explicit experience requirement was found in this posting."
    experience_known: bool = False
    blocker_status: str = "clear"  # clear / review / blocked
    geo_tags: tuple[str, ...] = ()

    @property
    def score_reasons(self) -> list[dict[str, Any]]:
        return [reason.as_dict() for reason in self.reasons]

    @property
    def positive_reasons(self) -> tuple[ScoreReason, ...]:
        """Only the evidence that argues the candidate qualifies."""

        return tuple(reason for reason in self.reasons if reason.delta > 0)

    @property
    def experience_unknown(self) -> bool:
        """True when the posting simply never stated an experience requirement.

        This is the case that must never be confused with "asks for too much".
        """

        return not self.experience_known

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "category": self.category,
            "reasons": self.score_reasons,
            "warnings": list(self.warnings),
            "uncertain": self.uncertain,
            "is_software_role": self.is_software_role,
            "hard_blockers": list(self.hard_blockers),
            "evidence_confidence": self.evidence_confidence,
            "entry_level_score": self.entry_level_score,
            "experience_category": self.experience_category,
            "experience_summary": self.experience_summary,
            "experience_detail": self.experience_detail,
            "experience_known": self.experience_known,
            "blocker_status": self.blocker_status,
            "geo_tags": list(self.geo_tags),
        }


_SENIOR_TITLE_RULES: tuple[tuple[str, str, int, bool], ...] = (
    (
        "manager_title",
        r"\b(?:engineering manager|manager,?\s+(?:software|engineering)|director|"
        r"head of|vice president|vp)\b",
        -90,
        True,
    ),
    ("principal_title", r"\b(?:principal|distinguished|architect)\b", -90, True),
    ("staff_title", r"\bstaff\b", -80, True),
    ("senior_title", r"\b(?:senior|sr\.?)\b", -70, True),
    ("lead_title", r"\b(?:lead|tech(?:nical)? lead)\b", -60, True),
    (
        "senior_level_title",
        r"\b(?:software\s+engineer|engineer|swe)\s+(?:iii|iv|3|4)\b",
        -70,
        True,
    ),
)

_BACHELORS_CS = re.compile(
    r"\b(?:bachelor(?:'s|s)?|b\.?s\.?)\s+(?:degree\s+)?(?:in\s+)?"
    r"(?:computer science|cs|software engineering|computer engineering|"
    r"a related (?:technical )?field)\b|"
    r"\bdegree in (?:computer science|cs|software engineering|computer engineering|"
    r"a related (?:technical )?field)\b"
)
_BACHELOR_OF_SCIENCE_CS = re.compile(
    r"\bbachelor of science\s+(?:degree\s+)?(?:in\s+)?"
    r"(?:computer science|software engineering|computer engineering|a related field)\b"
)
_INTERNSHIP_ACCEPTED = re.compile(
    r"\b(?:internship(?:s)?|co-?op|academic projects?)\s+(?:experience\s+)?"
    r"(?:is\s+)?(?:accepted|counts?|qualif(?:y|ies)|welcome|considered|a plus)\b|"
    r"\bexperience (?:through|from|including) (?:internships?|co-?ops?|academic projects?)\b|"
    r"\binternship or project experience\b"
)
def _experience_reason(evidence: ExperienceEvidence) -> ScoreReason:
    """Turn the experience verdict into one displayable score signal.

    The signal name *is* the evidence category, so the CSV, the dashboard and
    the filters all speak the same vocabulary.
    """

    return ScoreReason(
        evidence.category,
        evidence.delta,
        evidence.phrase or evidence.summary,
        evidence.confidence,
    )


def categorize_score(score: int, hard_ineligible: bool = False) -> str:
    """Map a clamped score to one of the four dashboard categories."""

    if hard_ineligible:
        return "likely_ineligible"
    if score >= STRONG_MATCH_SCORE:
        return "strong_match"
    if score >= POSSIBLE_MATCH_SCORE:
        return "possible_match"
    return "weak_match"


#: The graduation cohort the rules assume when a caller states none.  Kept in
#: one place so ``config.candidate.graduation_date`` and the rule defaults can
#: never silently disagree.
DEFAULT_GRADUATION_COHORT: tuple[int, int] = (2026, 5)


def score_job(
    title: str | None,
    description: str | None,
    location: str | None = None,
    workplace_type: str | None = None,
    employment_type: str | None = None,
    secondary_locations: Iterable[str] = (),
    *,
    software_classification: SoftwareClassification | None = None,
    eligibility: EligibilityAnalysis | None = None,
    geography: GeoAnalysis | None = None,
    location_context: str = "",
    candidate_graduation_year: int = DEFAULT_GRADUATION_COHORT[0],
    candidate_graduation_month: int = DEFAULT_GRADUATION_COHORT[1],
    department: str | None = None,
) -> ScoreResult:
    """Score candidate fit from 0 to 100 with non-overlapping evidence rules."""

    title_text = _clean(title)
    description_text = _clean(description)
    classification = software_classification or classify_software_role(
        title, description, department
    )
    eligibility_result = eligibility or analyze_eligibility(
        title,
        description,
        location,
        workplace_type,
        secondary_locations,
        candidate_graduation_year,
        candidate_graduation_month,
    )
    geo = geography or analyze_geography(
        location, workplace_type, secondary_locations, location_context, description
    )
    experience = classify_experience(title, description, eligibility_result.experience)
    reasons: list[ScoreReason] = []
    blockers: list[str] = []
    warnings: list[str] = []

    # Software classification is a gate, not a score.  It decides whether the
    # posting is considered at all and contributes nothing on its own.
    if not classification.is_software_role:
        blockers.append("not_software_role")
        reasons.append(ScoreReason("not_software_role", 0, title or None))
        warnings.append("Posting was not classified as a software-engineering role.")
        return ScoreResult(
            0,
            "likely_ineligible",
            tuple(reasons),
            tuple(warnings),
            classification.uncertain or eligibility_result.uncertain,
            False,
            tuple(blockers),
            LOW,
            entry_level_score=0,
            experience_category=experience.category,
            experience_summary=experience.summary,
            experience_detail=experience.detail,
            experience_known=experience.known,
            blocker_status="blocked",
            geo_tags=geo.tags,
        )
    # Two accumulators, because they answer different questions.  ``fit``
    # is how well this suits a new graduate; ``penalty`` is what would stop
    # the candidate taking it.  A blocked role can still be a perfect fit,
    # and the dashboard is more useful when it can say so.
    fit = 0
    penalty = 0

    # One experience verdict, positive or negative, covering both the wording
    # and any stated number.  Its category is what the dashboard filters on.
    reasons.append(_experience_reason(experience))
    fit += experience.delta
    if experience.hard_blocker:
        blockers.append(experience.category)
    if not experience.known:
        warnings.append(
            "No experience requirement was stated. This posting is not ruled out — "
            "it simply does not say — so read the requirements yourself."
        )

    # Likewise, one title-seniority penalty prevents "Senior Staff" double counts.
    senior_matches: list[tuple[int, str, str, bool]] = []
    for signal, pattern, delta, hard in _SENIOR_TITLE_RULES:
        match = re.search(pattern, title_text)
        if match:
            senior_matches.append((delta, signal, match.group(0), hard))
    if senior_matches:
        delta, signal, evidence, hard = min(senior_matches, key=lambda item: item[0])
        fit += delta
        reasons.append(ScoreReason(signal, delta, evidence, HIGH))
        if hard:
            blockers.append(signal)

    if eligibility_result.graduation.status == "compatible":
        fit += 35
        reasons.append(ScoreReason("compatible_2026_graduation_window", 35, None, MEDIUM))
    elif eligibility_result.graduation.status == "incompatible":
        penalty -= 70
        reasons.append(ScoreReason("incompatible_graduation_window", -70, None, HIGH))
        blockers.append("incompatible_graduation_window")

    # "All Levels" postings advertise a ladder.  Whether that ladder actually
    # reaches entry level is the whole question, so it is answered explicitly
    # rather than assumed either way.
    levels = eligibility_result.level_range
    if levels.multi_level:
        evidence_text = levels.reasons[0] if levels.reasons else None
        if levels.includes_entry is True:
            fit += 35
            reasons.append(
                ScoreReason("all_levels_includes_entry", 35, evidence_text, MEDIUM)
            )
        elif levels.includes_entry is False:
            fit -= 30
            reasons.append(
                ScoreReason("all_levels_excludes_entry", -30, evidence_text, MEDIUM)
            )
            warnings.append(
                "Posting advertises multiple levels but its stated range starts above "
                "entry level."
            )
        else:
            reasons.append(
                ScoreReason("all_levels_unspecified_range", 0, evidence_text, LOW)
            )
            warnings.append(
                "Posting advertises multiple levels without saying whether entry level "
                "is included; check the requirements before applying."
            )

    if _BACHELORS_CS.search(description_text) or _BACHELOR_OF_SCIENCE_CS.search(
        description_text
    ):
        fit += 5
        reasons.append(ScoreReason("bachelors_cs", 5, None, LOW))
    if _INTERNSHIP_ACCEPTED.search(description_text):
        fit += 12
        reasons.append(ScoreReason("internship_experience_accepted", 12, None, LOW))

    if re.search(r"\bintern(?:ship)?\b", title_text):
        fit -= 30
        reasons.append(ScoreReason("internship_role", -30, title or None, HIGH))
        if (
            eligibility_result.student_status.currently_enrolled_required
            and eligibility_result.student_status.status != "incompatible"
        ):
            penalty -= 80
            reasons.append(
                ScoreReason("current_enrollment_required_for_internship", -80, None, HIGH)
            )
            blockers.append("current_enrollment_required_for_internship")
    if eligibility_result.student_status.status == "incompatible":
        penalty -= 80
        reasons.append(ScoreReason("return_to_school_required", -80, None, HIGH))
        blockers.append("return_to_school_required")

    auth_flags = set(eligibility_result.work_authorization.flags)
    if "security_clearance_required" in auth_flags:
        penalty -= 100
        reasons.append(ScoreReason("security_clearance_required", -100, None, HIGH))
        blockers.append("security_clearance_required")
    elif auth_flags & {
        "citizenship_required",
        "permanent_resident_only",
        "us_person_required",
        "itar_export_control",
        "permanent_work_authorization_required",
        "opt_not_accepted",
    }:
        penalty -= 100
        if "opt_not_accepted" in auth_flags:
            signal = "opt_not_accepted"
        elif "permanent_work_authorization_required" in auth_flags:
            signal = "permanent_work_authorization_required"
        elif "itar_export_control" in auth_flags:
            signal = "itar_export_control"
        elif "us_person_required" in auth_flags:
            signal = "us_person_required"
        else:
            signal = "citizenship_or_permanent_residency_required"
        reasons.append(ScoreReason(signal, -100, None, HIGH))
        blockers.append(signal)
    elif auth_flags & {"no_sponsorship", "must_not_require_sponsorship"}:
        penalty -= 80
        reasons.append(ScoreReason("explicit_no_sponsorship", -80, None, HIGH))
        blockers.append("explicit_no_sponsorship")
    elif "sponsorship_available" in auth_flags:
        penalty += 5
        reasons.append(ScoreReason("sponsorship_available", 5, None, LOW))

    # Geography is a *filter*, not a ranking signal.  Collection is U.S.-wide, so
    # a role in Austin is not worth fewer points than one in Manhattan; the
    # reader decides which states they want by ticking boxes.  The tags are
    # recorded as a zero-delta signal purely so they are visible and searchable.
    if geo.tags:
        reasons.append(
            ScoreReason("us_location", 0, ", ".join(geo.labels), LOW)
        )
    elif geo.us_based is False:
        penalty -= 70
        reasons.append(ScoreReason("outside_united_states", -70, None, HIGH))
        blockers.append("outside_united_states")
    else:
        reasons.append(ScoreReason("location_not_stated", 0, None, LOW))

    # Preserve useful manual-review warnings, but do not turn missing sponsorship
    # text into a blocker.
    if eligibility_result.work_authorization.status == "uncertain":
        warnings.extend(eligibility_result.work_authorization.reasons)
    if eligibility_result.location.uncertain:
        warnings.extend(eligibility_result.location.reasons)
    if eligibility_result.student_status.status == "uncertain":
        warnings.extend(eligibility_result.student_status.reasons)
    if classification.uncertain:
        warnings.append("Software-role classification is borderline; review duties.")

    # Any explicit eligibility blocker discovered outside the scoring branches is
    # still honored.  This guards future extractors without silently changing score.
    if eligibility_result.likely_ineligible and not blockers:
        blockers.extend(eligibility_result.flags or ("explicit_eligibility_blocker",))

    # Confidence answers "how much does this posting actually tell us?", in
    # either direction.  "3+ years required" is high-confidence *negative*
    # evidence and should read that way; a posting that says nothing at all is
    # low confidence no matter how the score lands.  Keeping these separate is
    # what lets the dashboard distinguish a bad fit from an unknown one.
    substantive = [
        reason for reason in reasons if reason.confidence != LOW and reason.delta
    ]
    if substantive:
        evidence_confidence = max(
            (reason.confidence for reason in substantive),
            key=lambda value: _CONFIDENCE_ORDER[value],
        )
    else:
        evidence_confidence = LOW

    positive_substantive = [reason for reason in substantive if reason.delta > 0]
    if not positive_substantive and experience.known:
        # A location tag or a degree mention is context, not evidence that an
        # entry-level candidate qualifies.  Say so plainly rather than letting
        # the dashboard imply the role was matched on something meaningful.
        # When experience is *unknown*, the unknown warning already said it
        # better, and repeating it as "no evidence" would read as a rejection.
        reasons.append(ScoreReason("no_entry_level_evidence", 0, title or None, LOW))
        if not blockers:
            warnings.append(
                "Nothing in this posting says an entry-level candidate qualifies; "
                "read the requirements before spending time on it."
            )

    # Entry-level fit stands on its own, before eligibility is considered.
    entry_level_score = max(0, min(100, fit))
    hard_ineligible = bool(blockers)
    score = max(0, min(100, fit + penalty))
    if hard_ineligible:
        # Entry-level fit is moot once something disqualifies the candidate.
        # Capping keeps a blocked posting from outranking a real match when the
        # dashboard sorts by score, without erasing the fit itself.
        score = min(score, BLOCKED_SCORE_CEILING)
    category = categorize_score(score, hard_ineligible)
    uncertain = classification.uncertain or eligibility_result.uncertain
    if evidence_confidence == LOW and category != "likely_ineligible":
        uncertain = True
    if hard_ineligible:
        blocker_status = "blocked"
    elif warnings or uncertain:
        blocker_status = "review"
    else:
        blocker_status = "clear"
    return ScoreResult(
        score,
        category,
        tuple(reasons),
        tuple(dict.fromkeys(warnings)),
        uncertain,
        classification.is_software_role,
        tuple(dict.fromkeys(blockers)),
        evidence_confidence,
        entry_level_score=entry_level_score,
        experience_category=experience.category,
        experience_summary=experience.summary,
        experience_detail=experience.detail,
        experience_known=experience.known,
        blocker_status=blocker_status,
        geo_tags=geo.tags,
    )


def _location_evidence(job: Any) -> tuple[str, ...]:
    """Combine display locations with structured address text for location rules."""

    values = list(getattr(job, "secondary_locations", ()) or ())
    context = getattr(job, "location_context", "") or ""
    if context:
        values.append(context)
    return tuple(str(value) for value in values if value)


def score_normalized_job(job: Any) -> ScoreResult:
    """Score a ``NormalizedJob`` or any object exposing equivalent attributes."""

    return score_job(
        getattr(job, "title", ""),
        getattr(job, "description_text", ""),
        getattr(job, "location", None),
        getattr(job, "workplace_type", None),
        getattr(job, "employment_type", None),
        _location_evidence(job),
        geography=geography_from_job(job),
        department=getattr(job, "department", None),
    )


def analyze_normalized_job(
    job: Any,
    *,
    attach: bool = True,
    graduation_cohort: tuple[int, int] = DEFAULT_GRADUATION_COHORT,
) -> ScoreResult:
    """Analyze a normalized job and optionally attach persistence-ready fields.

    The shared ``NormalizedJob`` is intentionally mutable for this pipeline stage.
    Setting ``attach=False`` keeps this helper useful with read-only fixtures.

    ``graduation_cohort`` is ``(year, month)`` and comes from
    ``candidate.graduation_date``.  It only affects postings that state a
    graduation requirement; everything else scores identically regardless.
    """

    graduation_year, graduation_month = graduation_cohort
    locations = _location_evidence(job)
    classification = classify_software_role(
        getattr(job, "title", ""),
        getattr(job, "description_text", ""),
        getattr(job, "department", None),
    )
    eligibility = analyze_eligibility(
        getattr(job, "title", ""),
        getattr(job, "description_text", ""),
        getattr(job, "location", None),
        getattr(job, "workplace_type", None),
        locations,
        graduation_year,
        graduation_month,
    )
    geo = geography_from_job(job)
    result = score_job(
        getattr(job, "title", ""),
        getattr(job, "description_text", ""),
        getattr(job, "location", None),
        getattr(job, "workplace_type", None),
        getattr(job, "employment_type", None),
        locations,
        software_classification=classification,
        eligibility=eligibility,
        geography=geo,
        candidate_graduation_year=graduation_year,
        candidate_graduation_month=graduation_month,
        department=getattr(job, "department", None),
    )
    if attach:
        job.is_software = result.is_software_role
        job.search_score = result.score
        job.eligibility_category = result.category
        job.match_category = result.category
        job.eligibility_flags = tuple(
            dict.fromkeys((*eligibility.flags, *result.hard_blockers))
        )
        job.score_reasons = tuple(reason.as_dict() for reason in result.reasons)
        job.authorization_flag = eligibility.authorization_flag
        job.uncertain = result.uncertain
        job.evidence_confidence = result.evidence_confidence
        job.entry_level_score = result.entry_level_score
        job.experience_category = result.experience_category
        job.experience_summary = result.experience_summary
        job.experience_detail = result.experience_detail
        job.blocker_status = result.blocker_status
        job.geo_tags = result.geo_tags
    return result


def score_mapping(job: Mapping[str, Any]) -> ScoreResult:
    """Dictionary equivalent of :func:`score_normalized_job`."""

    return score_job(
        job.get("title"),
        job.get("description_text") or job.get("description") or job.get("description_html"),
        job.get("location"),
        job.get("workplace_type"),
        job.get("employment_type"),
        job.get("secondary_locations") or (),
        location_context=str(job.get("location_context") or ""),
        department=job.get("department"),
    )


calculate_score = score_job
score_job_fields = score_job
attach_job_analysis = analyze_normalized_job


__all__ = [
    "DEFAULT_GRADUATION_COHORT",
    "ScoreReason",
    "ScoreResult",
    "analyze_normalized_job",
    "attach_job_analysis",
    "calculate_score",
    "categorize_score",
    "score_job",
    "score_job_fields",
    "score_mapping",
    "score_normalized_job",
]
