"""Explainable extraction of job-eligibility signals.

Nothing in this module attempts to give legal or immigration advice.  It records
what a posting explicitly says and preserves uncertainty when wording is vague.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import html
import re
from typing import Any, Iterable


def _clean(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    # Typographic punctuation is everywhere in real postings: "we can’t sponsor
    # visas" must read the same as "we can't sponsor visas", and an em dash has
    # to separate clauses the way a hyphen does.
    value = value.replace("’", "'").replace("‘", "'")
    value = value.replace("“", '"').replace("”", '"')
    value = re.sub(r"[–—]", " - ", value)
    return re.sub(r"\s+", " ", value).strip().lower()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class ExperienceRequirement:
    min_years: int
    max_years: int | None
    requirement: str  # required, preferred, or uncertain
    phrase: str

    @property
    def required(self) -> bool:
        return self.requirement == "required"

    @property
    def preferred(self) -> bool:
        return self.requirement == "preferred"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SeniorityAnalysis:
    level: str
    signals: tuple[str, ...]
    likely_entry_level: bool
    likely_senior: bool
    uncertain: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorkAuthorizationAnalysis:
    status: str  # unspecified, positive, uncertain, likely_ineligible
    flags: tuple[str, ...]
    reasons: tuple[str, ...]
    explicit_blocker: bool
    uncertain: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GraduationAnalysis:
    status: str  # unspecified, compatible, uncertain, incompatible
    compatible: bool | None
    mentioned_years: tuple[int, ...]
    flags: tuple[str, ...]
    reasons: tuple[str, ...]
    uncertain: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StudentStatusAnalysis:
    status: str  # unspecified, compatible, uncertain, incompatible
    currently_enrolled_required: bool
    return_to_school_required: bool
    reasons: tuple[str, ...]
    uncertain: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LocationAnalysis:
    category: str  # nyc_nj, remote_us, other_us, remote_unspecified, non_us, unknown
    priority: int
    us_eligible: bool | None
    remote: bool
    reasons: tuple[str, ...]
    uncertain: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EligibilityAnalysis:
    experience: tuple[ExperienceRequirement, ...]
    seniority: SeniorityAnalysis
    work_authorization: WorkAuthorizationAnalysis
    graduation: GraduationAnalysis
    student_status: StudentStatusAnalysis
    location: LocationAnalysis
    level_range: LevelRangeAnalysis
    likely_ineligible: bool
    flags: tuple[str, ...]
    reasons: tuple[str, ...]
    uncertain: bool

    @property
    def authorization_flag(self) -> str:
        return self.work_authorization.status

    def as_dict(self) -> dict[str, Any]:
        return {
            "experience": [item.as_dict() for item in self.experience],
            "seniority": self.seniority.as_dict(),
            "work_authorization": self.work_authorization.as_dict(),
            "graduation": self.graduation.as_dict(),
            "student_status": self.student_status.as_dict(),
            "location": self.location.as_dict(),
            "level_range": self.level_range.as_dict(),
            "likely_ineligible": self.likely_ineligible,
            "flags": list(self.flags),
            "reasons": list(self.reasons),
            "uncertain": self.uncertain,
        }


_YEAR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?P<low>\d{1,2})\s*(?:-|–|—|to)\s*(?P<high>\d{1,2})\s*"
        r"(?:\+\s*)?(?:years?|yrs?)(?:\s+of)?\b"
    ),
    re.compile(
        r"\bbetween\s+(?P<low>\d{1,2})\s+and\s+(?P<high>\d{1,2})\s+"
        r"(?:years?|yrs?)(?:\s+of)?\b"
    ),
    re.compile(
        r"\b(?P<minimum>\d{1,2})\s*\+\s*(?:years?|yrs?)(?:\s+of)?\b"
    ),
    re.compile(
        r"\b(?:at\s+least|minimum(?:\s+of)?)\s+(?P<minimum>\d{1,2})\s+"
        r"(?:years?|yrs?)(?:\s+of)?\b"
    ),
    re.compile(
        r"\b(?:up\s+to|less\s+than|fewer\s+than)\s+(?P<maximum>\d{1,2})\s+"
        r"(?:years?|yrs?)(?:\s+of)?\b"
    ),
    re.compile(
        r"\b(?P<minimum>\d{1,2})\s+(?:years?|yrs?)\s+(?:of\s+)?"
        r"(?:(?:professional|industry|relevant|work|software|engineering|technical|"
        r"hands-on)\s+){0,3}"
        r"experience\b"
    ),
    re.compile(
        r"\bexperience\s*(?:of|:)?\s*(?P<minimum>\d{1,2})\s*(?:\+\s*)?"
        r"(?:years?|yrs?)\b"
    ),
    re.compile(r"\b(?P<minimum>\d{1,2})\s+(?:years?|yrs?)\b"),
)
_EXPERIENCE_CONTEXT = re.compile(
    r"\b(?:experience|experienced|professional|industry|engineering|software|developer|"
    r"programming|coding|building|developing|designing|python|java|javascript|typescript|"
    r"c\+\+|cloud|systems?|applications?|degree|bachelor)\b"
)
_NON_EXPERIENCE_CONTEXT = re.compile(
    r"\b(?:age|aged|older|vesting|vested|401k|leave|vacation|anniversary|ago|"
    r"contract(?: term| duration)?|temporary assignment)\b"
)
# ---------------------------------------------------------------------------
# Experience attribution
#
# A duration only becomes a *requirement* when the experience belongs to the
# applicant.  Job descriptions are full of durations that belong to somebody
# else — "Prophet Security's founding team brings more than 30 years of
# cybersecurity experience" is a boast about the founders, and reading it as a
# thirty-year requirement turns an explicit new-grad posting into a hard
# blocker.
#
# So each match is attributed rather than pattern-matched: look at the clause
# leading up to the number, find the marker nearest to it, and let that decide.
# Whichever wins — a third-party subject or a candidate-requirement cue — is the
# one the sentence is actually about.
# ---------------------------------------------------------------------------

# "our", "the", "their", or any possessive company name ("Prophet Security's").
# The gap is one word at most: "our founding team", "our small team", but not
# "our ideal staff security engineer", which is a description of the applicant.
_COMPANY_POSSESSIVE = r"(?:our|the|their|its|his|her|[a-z0-9][\w.&'-]*'s)\s+(?:\w+\s+){0,1}"
# Collectives and roles that are plainly not the applicant.  "candidate",
# "applicant", "engineer" and "you" are deliberately absent: a posting saying
# "our ideal engineer will have 5+ years" is describing the reader.
_THIRD_PARTY_NOUN = (
    r"(?:founding\s+team|leadership\s+team|executive\s+team|management\s+team|"
    r"exec\s+team|teams?|staff|founders?|leadership|executives?|advisors?|"
    r"advisers?|investors?|operators?|backers?|board|company|organi[sz]ation|"
    r"ceo|cto|cfo|coo|cpo|ciso|veterans?|alumni)"
)
# Verbs used when describing experience somebody has *accumulated*.  Copulas
# and prepositions are excluded: "our engineers are ..." and "team of ..." are
# far too common to carry meaning here.
_THIRD_PARTY_VERB = (
    r"(?:has|have|had|brings?|bring|brought|boasts?|combines?|combined|spent|"
    r"spans?|spanning|shares?|shared|contributes?|carr(?:y|ies|ied)|"
    r"averages?|averaging|accumulated|amassed|includes?|including)"
)
_THIRD_PARTY_ATTRIBUTION = re.compile(
    # "our founding team brings", "Prophet Security's founders have"
    rf"\b{_COMPANY_POSSESSIVE}{_THIRD_PARTY_NOUN}\s+(?:\w+\s+){{0,1}}?{_THIRD_PARTY_VERB}\b"
    # Bare plural subject with a plural verb: "founders bring".  Restricted to
    # subjects that can only be the company's own people — "engineers have" and
    # "people who have" describe candidates just as often.
    r"|\b(?:founders|executives|advisors|advisers|investors|operators|backers)\s+"
    r"(?:\w+\s+){0,1}?"
    r"(?:have|bring|brought|share|contribute|average|carry|combine|span|boast|"
    r"accumulated|amassed)\b"
    # Somebody else's experience the applicant is exposed to, not asked for.
    r"|\b(?:backed|advised|mentored|funded)\s+by\b"
    r"|\b(?:learn|learning)\s+(?:from|alongside)\b"
    r"|\btogether,?\s+(?:our|the|we)\b"
    # Explicitly pooled experience is never one applicant's requirement.
    r"|\b(?:combined|collective(?:ly)?|cumulative)\s+"
    r"(?:\w+\s+){0,2}?(?:experience|years)\b"
    r"|\b(?:years|experience)\s+(?:of\s+)?(?:combined|collective|cumulative)\b"
    # Company history: "Founded in 2017, we have spent 8 years building this."
    # Narrow on purpose — a bare "building" is far too common in duty prose to
    # carry any attribution meaning.
    r"|\bfounded\s+in\s+(?:19|20)\d{2}"
    r"|\bwe(?:'ve|\s+have)\s+(?:\w+\s+){0,2}?"
    r"(?:spent|been\s+(?:building|operating|shipping|serving|around))\b"
    r"|\b(?:the\s+)?company\s+(?:has\s+)?(?:been|spent)\b"
)

#: How far back a subject may sit and still govern a duration.
_ATTRIBUTION_LOOKBACK = 160

# Cues that the duration is being asked of the reader.  These override a
# third-party phrase when they sit closer to the number, which keeps sentences
# like "working with our engineers, you must have 5+ years" correct.
_CANDIDATE_ATTRIBUTION = re.compile(
    r"\byou(?:'ll|'ve| will| have| bring| need| should| must)?\b"
    r"|\byour\b|\bcandidates?\b|\bapplicants?\b"
    r"|\bwe(?:'re| are)?\s+(?:seeking|looking\s+for|hiring|require|need|want)\b"
    r"|\brequire(?:s|d|ment|ments)?\b|\bminimum\b|\bat\s+least\b|\bmust\s+have\b"
    r"|\bqualifications?\b|\bideal\s+candidate\b|\bsomeone\s+with\b"
    r"|\bprofessional\s+experience\b|\brelevant\s+experience\b"
    r"|\bproven\s+(?:track\s+record|experience)\b|\bexperience\s+required\b"
    r"|\bseeking\s+(?:an?\s+)?\w+\s+with\b|\blooking\s+for\b"
)
_PREFERRED_CONTEXT = re.compile(
    r"\b(?:preferred|ideally|nice to have|bonus|desired|a plus|would be a plus)\b"
)
_REQUIRED_CONTEXT = re.compile(
    r"\b(?:require[sd]?|requirements?|must|minimum|at least|need(?:s|ed)?|you have)\b"
)


def _clause_before(source: str, position: int) -> str:
    """Text from the start of the current clause up to ``position``.

    Attribution is decided within a clause rather than a fixed character
    window, so a sentence three lines earlier can never claim a number, and a
    long subject ("Prophet Security's founding team brings more than ") is
    never truncated away.
    """

    # Bullet lists rarely contain sentence punctuation, and normalization has
    # already collapsed their newlines, so the separators that survive — " - ",
    # " • " — have to count as boundaries too.  Without that, one boast at the
    # top of a requirements list would capture every number below it.
    boundary = max(
        source.rfind(".", 0, position),
        source.rfind(";", 0, position),
        source.rfind("!", 0, position),
        source.rfind("?", 0, position),
        source.rfind(":", 0, position),
        source.rfind("\n", 0, position),
        source.rfind("•", 0, position),
        source.rfind(" - ", 0, position),
        source.rfind(" – ", 0, position),
        source.rfind(" — ", 0, position),
    )
    # A hard look-back cap, because a run-on paragraph is not evidence that a
    # subject sixty words earlier governs this number.
    start = max(boundary + 1, position - _ATTRIBUTION_LOOKBACK)
    return source[start:position]


def _belongs_to_applicant(source: str, position: int) -> bool:
    """Decide whether a duration at ``position`` is being asked of the reader.

    Whichever attribution marker comes *first* in the clause governs it, because
    that is the clause's subject.  "You have a degree, backed by 5+ years of
    experience" is a requirement — the subject is "you", even though "backed by"
    sits nearer the number.  "Backed by operators with 20+ years" is not.

    With no marker at all the default is to accept, because requirement bullets
    routinely name no subject ("5+ years of backend experience").
    """

    clause = _clause_before(source, position)
    if not clause:
        return True
    third_party = _THIRD_PARTY_ATTRIBUTION.search(clause)
    if third_party is None:
        return True
    candidate = _CANDIDATE_ATTRIBUTION.search(clause)
    return candidate is not None and candidate.start() < third_party.start()


def experience_attribution(text: str | None) -> list[dict[str, Any]]:
    """Explain, per duration found, who it was attributed to.

    Exposed for auditing: it is the only way to see why a number in a posting
    was or was not treated as a requirement.
    """

    source = _clean(text)
    seen: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    for pattern in _YEAR_PATTERNS:
        for match in pattern.finditer(source):
            if any(match.start() < end and match.end() > start for start, end in spans):
                continue
            spans.append(match.span())
            clause = _clause_before(source, match.start())
            seen.append(
                {
                    "phrase": match.group(0),
                    "applicant": _belongs_to_applicant(source, match.start()),
                    "clause": clause[-120:],
                }
            )
    return seen


def extract_year_requirements(text: str | None) -> tuple[ExperienceRequirement, ...]:
    """Extract credible years-of-experience statements.

    A nearby ``preferred`` label wins over generic requirements wording.  Bare
    durations in compensation and benefits prose are discarded.
    """

    source = _clean(text)
    found: list[tuple[int, ExperienceRequirement]] = []
    occupied: list[tuple[int, int]] = []
    for pattern in _YEAR_PATTERNS:
        for match in pattern.finditer(source):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            low_raw = match.groupdict().get("low")
            high_raw = match.groupdict().get("high")
            minimum_raw = match.groupdict().get("minimum")
            maximum_raw = match.groupdict().get("maximum")
            minimum = int(low_raw or minimum_raw or 0)
            maximum = int(high_raw or maximum_raw) if high_raw or maximum_raw else None
            if minimum > 50 or (maximum is not None and maximum > 50):
                continue
            if maximum is not None and maximum < minimum:
                minimum, maximum = maximum, minimum

            nearby = source[max(0, match.start() - 130) : min(len(source), match.end() + 130)]
            # All broad duration patterns need evidence that the duration concerns
            # qualifications, while explicit "years of experience" always passes.
            phrase = match.group(0)
            explicit_experience = "experience" in phrase
            if (
                not explicit_experience
                and not _EXPERIENCE_CONTEXT.search(nearby)
                and not _PREFERRED_CONTEXT.search(nearby)
                and not _REQUIRED_CONTEXT.search(nearby)
            ):
                continue
            if _NON_EXPERIENCE_CONTEXT.search(nearby) and not explicit_experience:
                continue
            # Whose experience is this?  A duration attributed to the founders
            # is not a requirement on the applicant.
            if not _belongs_to_applicant(source, match.start()):
                continue

            left_boundary = max(
                source.rfind(".", 0, match.start()), source.rfind(";", 0, match.start())
            )
            right_candidates = [
                boundary
                for boundary in (source.find(".", match.end()), source.find(";", match.end()))
                if boundary >= 0
            ]
            right_boundary = min(right_candidates) if right_candidates else len(source)
            before_clause = source[left_boundary + 1 : match.start()]
            after_clause = source[match.end() : right_boundary]
            # A trailing label ("3 years preferred") is most specific.  Otherwise
            # use the closest preceding section/phrase, which keeps collapsed
            # "Preferred Qualifications ... Minimum Qualifications ..." text sane.
            trailing_preferred = _PREFERRED_CONTEXT.search(after_clause[:80])
            trailing_required = _REQUIRED_CONTEXT.search(after_clause[:80])
            if trailing_preferred and re.match(
                r"preferred\s+qualifications?\b",
                after_clause[trailing_preferred.start() :],
            ):
                trailing_preferred = None
            if trailing_required and re.match(
                r"(?:minimum|required|basic)\s+qualifications?\b|requirements?\s*:",
                after_clause[trailing_required.start() :],
            ):
                trailing_required = None
            preceding_preferred = list(_PREFERRED_CONTEXT.finditer(before_clause))
            preceding_required = list(_REQUIRED_CONTEXT.finditer(before_clause))
            if trailing_preferred and (
                not trailing_required or trailing_preferred.start() <= trailing_required.start()
            ):
                requirement = "preferred"
            elif trailing_required:
                requirement = "required"
            elif preceding_preferred and (
                not preceding_required
                or preceding_preferred[-1].start() >= preceding_required[-1].start()
            ):
                requirement = "preferred"
            else:
                # Job requirement bullets commonly omit the word "required".
                # Treat a concrete experience duration as required unless the
                # surrounding prose explicitly labels it preferred/optional.
                requirement = "required"
            found.append(
                (
                    match.start(),
                    ExperienceRequirement(minimum, maximum, requirement, phrase),
                )
            )
            occupied.append(match.span())
    found.sort(key=lambda item: item[0])
    deduplicated: list[ExperienceRequirement] = []
    seen: set[tuple[int, int | None, str, str]] = set()
    for _, item in found:
        key = (item.min_years, item.max_years, item.requirement, item.phrase)
        if key not in seen:
            seen.add(key)
            deduplicated.append(item)
    return tuple(deduplicated)


extract_experience_requirements = extract_year_requirements


_SENIORITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("executive", r"\b(?:chief|vice president|vp|head of)\b"),
    ("director", r"\bdirector\b"),
    ("manager", r"\bmanager\b"),
    ("distinguished", r"\bdistinguished\b"),
    ("principal", r"\bprincipal\b"),
    ("staff", r"\bstaff\b"),
    ("lead", r"\b(?:tech(?:nical)?\s+lead|lead)\b"),
    ("architect", r"\barchitect\b"),
    ("senior", r"\b(?:senior|sr\.?)\b"),
    ("junior", r"\b(?:junior|jr\.?)\b"),
    ("entry", r"\b(?:entry[ -]?level|new grad(?:uate)?|early career|graduate)\b"),
    ("associate", r"\bassociate\b"),
)
_SENIOR_LEVELS = {
    "executive",
    "director",
    "manager",
    "distinguished",
    "principal",
    "staff",
    "lead",
    "architect",
    "senior",
}


def detect_seniority(
    title: str | None,
    description: str | None = None,
    experience: Iterable[ExperienceRequirement] | None = None,
) -> SeniorityAnalysis:
    """Infer title seniority, using required experience only as a fallback signal."""

    normalized_title = _clean(title)
    signals: list[str] = []
    levels: list[str] = []
    for level, pattern in _SENIORITY_PATTERNS:
        if re.search(pattern, normalized_title):
            levels.append(level)
            signals.append(f"{level}_title")
    if re.search(r"\b(?:software\s+)?engineer\s+(?:i|1)\b", normalized_title):
        levels.append("entry")
        signals.append("level_one_title")
    elif re.search(r"\b(?:software\s+)?engineer\s+(?:ii|2)\b", normalized_title):
        levels.append("mid")
        signals.append("level_two_title")
    elif re.search(r"\b(?:software\s+)?engineer\s+(?:iii|3|iv|4)\b", normalized_title):
        levels.append("senior")
        signals.append("level_three_plus_title")
    if re.search(r"\bswe\s+(?:i|1)\b", normalized_title):
        levels.append("entry")
        signals.append("swe_level_one_title")
    elif re.search(r"\bswe\s+(?:ii|2)\b", normalized_title):
        levels.append("mid")
        signals.append("swe_level_two_title")
    elif re.search(r"\bswe\s+(?:iii|3|iv|4)\b", normalized_title):
        levels.append("senior")
        signals.append("swe_level_three_plus_title")

    requirements = (
        tuple(experience) if experience is not None else extract_year_requirements(description)
    )
    required_minima = [item.min_years for item in requirements if item.required]
    if required_minima:
        maximum_required = max(required_minima)
        if maximum_required >= 5:
            signals.append("five_plus_years_required")
        elif maximum_required >= 3:
            signals.append("three_plus_years_required")
        elif maximum_required <= 2:
            signals.append("zero_to_two_years")

    order = {
        "unknown": 0,
        "entry": 1,
        "junior": 1,
        "associate": 1,
        "mid": 2,
        "senior": 3,
        "lead": 4,
        "architect": 5,
        "staff": 5,
        "principal": 6,
        "distinguished": 7,
        "manager": 8,
        "director": 9,
        "executive": 10,
    }
    level = max(levels, key=lambda value: order[value]) if levels else "unknown"
    likely_senior = level in _SENIOR_LEVELS or bool(required_minima and max(required_minima) >= 5)
    likely_entry = level in {"entry", "junior", "associate"} or (
        not likely_senior and bool(required_minima) and max(required_minima) <= 2
    )
    uncertain = level == "unknown" and not required_minima
    return SeniorityAnalysis(level, _unique(signals), likely_entry, likely_senior, uncertain)


@dataclass(frozen=True, slots=True)
class LevelRangeAnalysis:
    """What an 'all levels' / multi-level posting actually accepts."""

    multi_level: bool
    includes_entry: bool | None  # None means the posting does not say
    lowest_level: str | None
    reasons: tuple[str, ...]

    @property
    def status(self) -> str:
        if not self.multi_level:
            return "not_multi_level"
        if self.includes_entry is None:
            return "undetermined"
        return "includes_entry" if self.includes_entry else "excludes_entry"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_MULTI_LEVEL = re.compile(
    r"\ball\s+(?:experience\s+)?levels?\b|\bany\s+level\b|"
    r"\bmultiple\s+(?:experience\s+)?levels?\b|"
    r"\bhiring\s+(?:across|at)\s+(?:multiple\s+|all\s+)?(?:experience\s+)?levels?\b|"
    r"\bwe\s+hire\s+at\s+all\s+levels\b|\ball[- ]levels\b"
)
# Ladder rungs an employer lists when it advertises several levels at once.
_ENTRY_RUNG = re.compile(
    r"\b(?:associate|junior|jr\.?|entry[- ]level|new\s+grad(?:uate)?|graduate|"
    r"(?:software\s+)?engineer\s+(?:i|1)\b|swe\s*(?:i|1)\b|l[1-3]\b|ic[1-2]\b)"
)
_MID_OR_ABOVE_RUNG = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|distinguished|lead|architect|mid[- ]level|"
    r"(?:software\s+)?engineer\s+(?:iii|iv|3|4)\b|l[5-9]\b|ic[4-9]\b)"
)
_EXPLICIT_ENTRY_WELCOME = re.compile(
    r"\bnew\s+grad(?:uate)?s?\b|\brecent\s+graduates?\b|\bentry[- ]level\b|"
    r"\bfrom\s+(?:new\s+grad|entry[- ]level|associate|junior)\b|"
    r"\b(?:associate|junior)\s+through\s+\w+"
)
# Pay boilerplate ("our base salary range reflects multiple levels") describes
# the compensation table, not who the role is open to.
_COMPENSATION_BOILERPLATE = re.compile(
    r"\b(?:base salary|salary range|salary ranges|compensation range|pay range|"
    r"salary bands?|base pay|total compensation)\b"
)
_EXPLICIT_MID_FLOOR = re.compile(
    r"\bmid[- ]level\s+to\s+senior\b|\bsenior\s+(?:to|through)\s+(?:staff|principal)\b|"
    r"\bfrom\s+senior\b|\bmid\s+to\s+senior\b"
)


def _multi_level_claim(text: str) -> bool:
    """True when the posting says the *role* spans levels, not just its pay band."""

    for match in _MULTI_LEVEL.finditer(text):
        window = text[max(0, match.start() - 150) : match.end() + 60]
        if _COMPENSATION_BOILERPLATE.search(window):
            continue
        return True
    return False


def detect_level_range(
    title: str | None,
    description: str | None = None,
    experience: Iterable[ExperienceRequirement] | None = None,
) -> LevelRangeAnalysis:
    """Decide whether a multi-level posting genuinely reaches entry level.

    Employers advertise "All Levels" both ways: some enumerate a ladder that
    starts at Associate, others attach it to a role that still demands several
    years.  Required experience wins over the advertised ladder, because that is
    the line the recruiter actually screens on.
    """

    title_text = _clean(title)
    body = _clean(description)
    combined = f"{title_text} {body}".strip()
    if not _multi_level_claim(combined):
        return LevelRangeAnalysis(False, None, None, ())

    reasons: list[str] = []
    requirements = (
        tuple(experience) if experience is not None else extract_year_requirements(description)
    )
    required_minima = [item.min_years for item in requirements if item.required]
    if required_minima and max(required_minima) >= 3:
        reasons.append(
            "Posting advertises several levels but still requires "
            f"{max(required_minima)}+ years of experience."
        )
        return LevelRangeAnalysis(True, False, "mid", tuple(reasons))

    if _EXPLICIT_MID_FLOOR.search(combined):
        reasons.append("Posting states its range starts above entry level.")
        return LevelRangeAnalysis(True, False, "mid", tuple(reasons))

    if _EXPLICIT_ENTRY_WELCOME.search(combined):
        reasons.append("Posting explicitly names an entry-level rung in its range.")
        return LevelRangeAnalysis(True, True, "entry", tuple(reasons))

    entry_rung = _ENTRY_RUNG.search(combined)
    mid_rung = _MID_OR_ABOVE_RUNG.search(combined)
    if entry_rung and (not mid_rung or entry_rung.start() <= mid_rung.start()):
        reasons.append(f"Advertised level ladder starts at {entry_rung.group(0)!r}.")
        return LevelRangeAnalysis(True, True, "entry", tuple(reasons))
    if mid_rung and not entry_rung:
        reasons.append(f"Advertised levels start at {mid_rung.group(0)!r}.")
        return LevelRangeAnalysis(True, False, "mid", tuple(reasons))

    if required_minima and max(required_minima) <= 2:
        reasons.append("Multi-level posting requires at most two years of experience.")
        return LevelRangeAnalysis(True, True, "entry", tuple(reasons))

    reasons.append("Posting advertises several levels but does not say how junior it goes.")
    return LevelRangeAnalysis(True, None, None, tuple(reasons))


level_range = detect_level_range

# ---------------------------------------------------------------------------
# Work authorization
#
# Real postings almost never say "we do not sponsor".  They say things like
# "at this time, we are unable to offer employment for individuals located
# outside of the United States and/or those who require sponsorship for
# employment visa status (e.g., H-1B visa status)" — eighty-nine characters
# between the refusal and the word it refuses.
#
# So detection is *scoped* rather than adjacent: split the posting into
# sentences, then look for a refusal that governs a sponsorship object inside
# the same sentence.  That reads the sentence the way a person does, and it
# survives the parentheticals, bullets and intervening clauses that broke the
# older adjacency patterns.
#
# The categories below stay separate on purpose.  "We won't sponsor a visa",
# "you must be a citizen", "this is ITAR-controlled" and "you need a clearance"
# are four different problems with four different answers, and collapsing them
# into one regex would lose the reason a job is out of reach.
# ---------------------------------------------------------------------------

# Sentence-ish segmentation.  Bullets and newlines matter as much as periods:
# postings put the restriction on its own line at least as often as in prose.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+|(?:(?<=\s)|^)[-•*·]\s+")

# Equal-opportunity boilerplate names "citizenship" and "immigration status" as
# protected characteristics.  It is the single largest false-positive risk in
# the corpus — around a hundred occurrences — and it never restricts anyone.
_EEO_BOILERPLATE = re.compile(
    r"\bdiscriminat\w*|\bequal (?:opportunity|employment)\b|\baffirmative action\b|"
    r"\bprotected (?:class|characteristic|veteran|status)\w*|\bharassment\b|"
    r"\bwithout regard to\b|\bregardless of\b|\ball qualified applicants\b"
)

# What is actually being refused.  Deliberately *not* bare "immigration": EEO
# text says "citizenship or immigration status", and "company sponsored 401(k)"
# must never read as sponsorship either — hence sponsor/sponsorship/sponsorships
# only, which cannot match "sponsored" or "sponsoring".
_SPONSORSHIP_OBJECT = re.compile(
    r"\bsponsor(?:ship|ships)?\b|"
    r"\bvisa support\b|\bimmigration support\b|"
    r"\b(?:employment|employment[- ]based|work)\s+visas?\b|"
    r"\bh-?1-?b\b"
)

# The company refusing to act.
_REFUSAL_TRIGGER = re.compile(
    r"\bun(?:able|willing)\s+to\b|\bnot\s+(?:able|willing|currently\s+able)\s+to\b|"
    r"\bcannot\b|\bcan'?t\b|\bwill\s+not\b|\bwon'?t\b|"
    r"\bdo(?:es)?\s+not\b|\bdon'?t\b|\bdoesn'?t\b|"
    r"\bno\s+longer\b|\bnot\s+(?:currently\s+)?(?:offering|providing|initiating|"
    r"sponsoring|able|eligible|available|open)\b|"
    r"\b(?:in)?eligible\b|\bunavailable\b|\bnot\s+available\b|\bnone\b|"
    r"\bunfortunately\b|\bnot\s+provide\b|\bnot\s+offer\b|\bnot\s+support\b"
)

# The candidate being required not to need sponsorship.  Checked before the
# refusal form because it is the more specific reading of the same words.
_REQUIREMENT_FORM = re.compile(
    r"\bwithout\s+(?:the\s+need\s+for\s+|any\s+|需?)?"
    r"(?:current(?:ly)?\s*(?:,|or|and|/)?\s*(?:or\s+)?future\s+)?"
    r"(?:restrictions?\s+or\s+)?"
    r"(?:visa\s+|employment\s+|employer\s+|immigration\s+|work\s+)?sponsorship\b|"
    r"\bmust\s+not\s+(?:now\s+or\s+in\s+the\s+future\s+)?require\s+"
    r"(?:visa\s+|employment\s+|employer\s+)?sponsorship\b|"
    r"\b(?:who|that)\s+do(?:es)?\s+not\s+require\s+(?:an?\s+)?"
    r"(?:visa\s+|employment\s+|employer\s+)?sponsorship\b|"
    r"\bno\s+(?:need\s+for\s+)?(?:current\s+(?:or|and)\s+future|future)\s+"
    r"(?:visa\s+|employment\s+|employer\s+)?sponsorship\b|"
    r"\bwill\s+not\s+require\s+sponsorship\s+(?:now|currently)\s+(?:or|and)\s+"
    r"in\s+the\s+future\b|"
    r"\bapplicants?\s+(?:requiring|who\s+require)\s+sponsorship\s+"
    r"(?:will\s+not\s+be\s+considered|cannot\s+be\s+considered|are\s+not\s+eligible)\b"
)

# "Visa sponsorship: not available" puts the object first, so the refusal is
# looked for after it as well as before it.
_TRAILING_REFUSAL = re.compile(
    r"^\s*(?::|-|–|is|are|will\s+be|would\s+be)?\s*(?:not\s+"
    r"(?:available|offered|provided|possible|supported)|unavailable|none|no)\b"
)
# A bare "no" is only a refusal when it sits directly on the object: postings
# write "local candidates only. no visa sponsorship." as a complete statement,
# with no verb anywhere.  Requiring adjacency keeps "no experience required"
# and similar from ever reaching this.
_NO_IMMEDIATELY_BEFORE = re.compile(r"\bno\s+(?:\w+\s+){0,2}$")

# "Candidates requiring sponsorship cannot be considered" also puts the object
# first, but separates it from the refusal by a clause.
_REFUSED_AFTER_OBJECT = re.compile(
    r"^[^.;]{0,60}?\b(?:cannot|can'?t|will\s+not|won'?t|are\s+not|is\s+not|"
    r"may\s+not|not)\s+(?:be\s+)?"
    r"(?:considered|accepted|eligible|hired|sponsored|supported|entertained)\b"
)

# Wording that looks like a refusal but is not one.  "We can't successfully
# sponsor for every role and every candidate, but if we make you an offer we'll
# make every reasonable effort" is a *sponsorship-friendly* posting, and nine
# live boards use almost exactly that sentence.
_SPONSORSHIP_HEDGE = re.compile(
    r"\bguarantee\w*\b|\bevery\s+(?:role|candidate|case|person|applicant)\b|"
    r"\bnot\s+limited\s+to\b|\bcase[- ]by[- ]case\b|\bmay\s+be\s+available\b|"
    r"\bopt[- ]?(?:out|in|ing\s+out)\b|\bdepend(?:s|ing)?\s+on\b|"
    # "applicants with or without sponsorship needs" is explicitly inclusive:
    # the word "without" here is half of a pair, not a requirement.
    r"\b(?:with\s+or\s+without|regardless\s+of|either\s+with\s+or\s+without)\s+"
    r"(?:visa\s+|employment\s+)?sponsorship\b"
)
_SPONSORSHIP_POSITIVE = re.compile(
    r"\b(?:visa|employment|immigration)\s+sponsorship\s+(?:is\s+)?"
    r"(?:available|offered|provided|possible)\b|"
    r"\bsponsorship\s+(?:is\s+)?available\b|\bsponsorship\s+available\b|"
    r"\bwe\s+(?:can|do|will|may|are\s+happy\s+to|are\s+able\s+to)\s+sponsor\b|"
    r"\bcan\s+sponsor\s+visas?\b|\bwe\s+sponsor\b|\bwe\s+offer\s+visa\s+sponsorship\b|"
    r"\bwe\s+(?:(?:can|do|will|may)\s+)?(?:offer|provide|support)\s+"
    r"(?:(?:visa|employment|immigration)\s+)?sponsorship\b|"
    r"\bopen\s+to\s+[^.;]{0,40}\bsponsor\w*\b|"
    r"\bimmigration\s+law\s+firm\b|\breasonable\s+effort\b|"
    r"\bcommitted\s+to\s+working\s+through\s+the\s+visa\b|"
    r"\bsuccessful\s+in\s+sponsoring\b|\bhave\s+sponsored\b|"
    r"\bsponsorship\s+(?:candidates?\s+)?(?:are\s+)?welcome\b|"
    r"\bwelcome\s+(?:candidates?\s+)?(?:requiring|who\s+require)\s+sponsorship\b"
)

_CITIZENSHIP_REQUIRED = re.compile(
    # A parenthetical or list marker frequently sits between the verb and the
    # noun: "must be a United States (U.S.) citizen", "must be a (i) U.S.
    # citizen or national".
    r"\b(?:must\s+be|only|restricted\s+to)\s+(?:an?\s+)?"
    r"(?:\(\s*[ivx\d]+\s*\)\s*)?(?:united\s+states|u\.?s\.?)\s*"
    r"(?:\([^)]{0,12}\)\s*)?(?:citizens?|nationals?)\b|"
    r"\b(?:u\.?s\.?|united\s+states)\s+citizens?\s+only\b|"
    r"\b(?:u\.?s\.?|united\s+states)\s+citizenship\s+(?:is\s+)?"
    r"(?:required|a\s+requirement|mandatory)\b|"
    r"\brequires?\s+(?:u\.?s\.?|united\s+states)\s+citizenship\b|"
    r"\bapplicants?\s+must\s+(?:hold|have|be)\s+"
    r"(?:an?\s+)?(?:u\.?s\.?|united\s+states)\s+cit\w+\b|"
    r"\bmust\s+be\s+(?:an?\s+)?(?:u\.?s\.?|united\s+states)\s+citizen\b"
)
_PERMANENT_RESIDENT_ONLY = re.compile(
    r"\b(?:citizens?|u\.?s\.?\s+citizens?)\s+(?:and|or)\s+(?:lawful\s+)?"
    r"permanent\s+residents?\s+only\b|"
    r"\b(?:green\s+card\s+holders?|permanent\s+residents?)\s+only\b|"
    r"\bmust\s+be\s+(?:an?\s+)?(?:u\.?s\.?\s+)?permanent\s+resident\b|"
    r"\bmust\s+(?:hold|have)\s+(?:a\s+)?green\s+card\b"
)
_US_PERSON_REQUIRED = re.compile(
    r"\bmust\s+(?:be|qualify\s+as)\s+(?:an?\s+)?u\.?s\.?\s+persons?\b|"
    r"\bu\.?s\.?\s+person\s+status\s+(?:is\s+)?required\b|"
    r"\brestricted\s+to\s+u\.?s\.?\s+persons?\b"
)
_EXPORT_CONTROL = re.compile(
    r"\bitar\b|\binternational\s+traffic\s+in\s+arms\b|"
    r"\bexport\s+administration\s+regulations\b|\bexport[- ]control(?:led|s)?\b|"
    r"\bexport\s+control\s+(?:laws?|regulations?|requirements?)\b"
)
# Export-control prose only disqualifies when it is tied to a person
# restriction.  "This position may require access to export-controlled
# technology" on its own is a notice, not a bar, and thirteen live postings
# use exactly that hedged wording.
_EXPORT_PERSON_RESTRICTION = re.compile(
    r"\bmust\s+be\s+(?:one\s+of\s+the\s+following|an?\s+u\.?s\.?\s+person|"
    r"an?\s+(?:\(\s*[ivx\d]+\s*\)\s*)?(?:u\.?s\.?|united\s+states))\b|"
    r"\bu\.?s\.?\s+person\b|\bcitizens?\s+or\s+(?:lawful\s+)?permanent\s+residents?\b"
)
_PERMANENT_WORK_AUTHORIZATION = re.compile(
    r"\b(?:permanent|unrestricted|indefinite)\s+(?:u\.?s\.?\s+)?"
    r"work\s+authorization\s+(?:is\s+)?required\b|"
    r"\bmust\s+(?:have|possess)\s+(?:permanent|unrestricted|indefinite)\s+"
    r"(?:u\.?s\.?\s+)?work\s+authorization\b"
)
# "opt out" is a cookie banner, not a visa status, and sixteen live postings
# say it.  The lookahead is what keeps those out.
_OPT_STATUS = re.compile(r"\b(?:stem\s+)?opt\b(?!\s*[- ]?(?:out|in))|\bcpt\b")
_OPT_EXCLUDED = re.compile(
    r"\b(?:opt|cpt)(?:\s+(?:or|and)\s+(?:opt|cpt))?\s+"
    r"(?:candidates?\s+)?(?:are\s+)?not\s+(?:accepted|eligible|considered)\b|"
    r"\b(?:do\s+not|don'?t|cannot|can'?t|unable\s+to|"
    r"not\s+(?:currently\s+)?able\s+to)\s+"
    r"(?:accept|consider|engage|hire|sponsor)\s+[^.;]{0,40}\b(?:opt|cpt)\b"
)
_CLEARANCE_REQUIRED = re.compile(
    r"\b(?:active|current)\s+(?:(?:u\.?s\.?)\s+)?"
    r"(?:secret|top\s+secret|ts\/sci|security)\s+clearance\s+required\b|"
    r"\b(?:must\s+)?(?:hold|possess|have|be\s+able\s+to\s+obtain|obtain\s+and\s+maintain|"
    r"ability\s+to\s+(?:gain|obtain))\s+"
    r"(?:and\s+maintain\s+)?(?:an?\s+)?(?:(?:u\.?s\.?|government[- ]issued|dod)\s+)?"
    r"(?:secret|top\s+secret|ts\/sci|security|government[- ]issued)\s+clearance\b|"
    r"\b(?:secret|top\s+secret|ts\/sci)\s+(?:security\s+)?clearance\s+(?:is\s+)?required\b|"
    r"\bsecurity\s+clearance\s+required\b"
)
_CLEARANCE_MENTIONED = re.compile(r"\b(?:security|government[- ]issued|dod)\s+clearance\b")
_CONDITIONAL = re.compile(r"\bif\s+(?:needed|required|applicable)\b|\bmay\s+(?:be\s+)?require")

_AUTH_GENERIC = re.compile(
    r"\b(?:legally\s+)?authorized\s+to\s+work\s+in\s+the\s+(?:u\.?s\.?|united\s+states)\b|"
    r"\bwork\s+authorization\s+(?:is\s+)?required\b|"
    r"\bemployment\s+eligibility\s+verification\b|"
    r"\bproof\s+of\s+citizenship\b"
)
_PUBLIC_TRUST = re.compile(r"\b(?:public trust|background investigation)\b")
# Any mention at all, used only to decide whether the dashboard may claim that
# sponsorship was "not mentioned".
_SPONSORSHIP_MENTIONED = re.compile(
    r"\bsponsor\w*\b|\bvisas?\b|\bh-?1-?b\b|\bimmigration\b|\bwork\s+authorization\b|"
    r"\bwork\s+authorisation\b|\bemployment\s+authorization\b|\bgreen\s+card\b|"
    r"\bpermanent\s+resident\b|\bauthorized\s+to\s+work\b"
)


def _sentences(source: str) -> list[str]:
    """Split cleaned text into sentence-like spans for scoped negation."""

    return [part.strip() for part in _SENTENCE_SPLIT.split(source) if part and part.strip()]


def _sponsorship_verdict(sentence: str) -> str | None:
    """Classify one sentence's sponsorship stance.

    Returns ``"no_sponsorship"``, ``"must_not_require_sponsorship"``,
    ``"positive"`` or ``None``.  The negation has to *govern* the sponsorship
    object — appear before it in the same sentence — which is what lets a
    refusal survive an intervening clause without matching every sentence that
    happens to contain both a "not" and the word "visa".
    """

    if _EEO_BOILERPLATE.search(sentence):
        return None
    objects = list(_SPONSORSHIP_OBJECT.finditer(sentence))
    if not objects:
        return None
    # Hedges are checked before anything else: "we can't guarantee your visa"
    # and "we can't sponsor for every role" are friendly sentences that happen
    # to contain a negation next to the word sponsor.
    if _SPONSORSHIP_HEDGE.search(sentence):
        return None
    if _REQUIREMENT_FORM.search(sentence):
        return "must_not_require_sponsorship"
    for match in objects:
        before, after = sentence[: match.start()], sentence[match.end() :]
        # A refusal anywhere earlier in the sentence governs this object.
        if _REFUSAL_TRIGGER.search(before) or _NO_IMMEDIATELY_BEFORE.search(before):
            return "no_sponsorship"
        # ...or the object is the subject: "visa sponsorship: not available",
        # "candidates requiring sponsorship cannot be considered".
        if _TRAILING_REFUSAL.match(after) or _REFUSED_AFTER_OBJECT.match(after):
            return "no_sponsorship"
    # Only once no refusal governs the sentence does friendly wording count.
    # Checking this first would read "this role is not open to those who require
    # sponsorship" as an offer of sponsorship, which is exactly backwards.
    if _SPONSORSHIP_POSITIVE.search(sentence):
        return "positive"
    return None


def detect_work_authorization(text: str | None) -> WorkAuthorizationAnalysis:
    """Find explicit authorization restrictions; silence is never rejection.

    Six outcomes are kept distinct because they mean different things to an
    applicant: an explicit sponsorship refusal, a citizenship/permanent-resident
    restriction, an export-control restriction, a clearance requirement,
    sponsorship explicitly offered, and no information at all.
    """

    source = _clean(text)
    flags: list[str] = []
    reasons: list[str] = []
    sentences = _sentences(source)

    # --- sponsorship, read sentence by sentence -----------------------------
    refuses = requires_no_sponsorship = offers = False
    for sentence in sentences:
        verdict = _sponsorship_verdict(sentence)
        if verdict == "no_sponsorship":
            refuses = True
        elif verdict == "must_not_require_sponsorship":
            requires_no_sponsorship = True
        elif verdict == "positive":
            offers = True
    if refuses:
        flags.append("no_sponsorship")
        reasons.append("Posting explicitly says sponsorship is unavailable.")
    if requires_no_sponsorship:
        flags.append("must_not_require_sponsorship")
        reasons.append("Posting requires work authorization without current/future sponsorship.")

    # --- status-based restrictions, each kept separate ----------------------
    non_eeo = " ".join(s for s in sentences if not _EEO_BOILERPLATE.search(s))
    if _CITIZENSHIP_REQUIRED.search(non_eeo):
        flags.append("citizenship_required")
        reasons.append("Posting explicitly requires U.S. citizenship.")
    if _PERMANENT_RESIDENT_ONLY.search(non_eeo):
        flags.append("permanent_resident_only")
        reasons.append("Posting restricts eligibility to citizens/permanent residents.")
    if _US_PERSON_REQUIRED.search(non_eeo):
        flags.append("us_person_required")
        reasons.append("Posting explicitly requires U.S.-person status for export-control purposes.")
    export_control = bool(_EXPORT_CONTROL.search(non_eeo))
    if export_control and _EXPORT_PERSON_RESTRICTION.search(non_eeo):
        flags.append("itar_export_control")
        reasons.append(
            "Posting is export-controlled (ITAR/EAR) and limits eligibility by immigration status."
        )
    if _PERMANENT_WORK_AUTHORIZATION.search(non_eeo):
        flags.append("permanent_work_authorization_required")
        reasons.append("Posting explicitly requires permanent/unrestricted U.S. work authorization.")
    if _OPT_EXCLUDED.search(non_eeo):
        flags.append("opt_not_accepted")
        reasons.append("Posting explicitly excludes OPT/CPT candidates.")
    # "if needed" has to be read against the clearance sentence itself.  Testing
    # the whole document meant one hedged aside elsewhere in a long posting
    # silently cancelled an unambiguous "Active Secret clearance required".
    clearance_sentences = [s for s in sentences if _CLEARANCE_REQUIRED.search(s)]
    clearance_required = bool(clearance_sentences)
    if any(not _CONDITIONAL.search(s) for s in clearance_sentences):
        flags.append("security_clearance_required")
        reasons.append("Posting requires an incompatible U.S. security clearance.")

    blocking = [flag for flag in flags if flag != "sponsorship_available"]
    mentioned = bool(_SPONSORSHIP_MENTIONED.search(source))
    generic = bool(_AUTH_GENERIC.search(source))
    public_trust = bool(_PUBLIC_TRUST.search(source))

    if blocking:
        status = "likely_ineligible"
        uncertain = False
    elif offers:
        status = "positive"
        flags.append("sponsorship_available")
        reasons.append("Posting explicitly indicates sponsorship may be available.")
        uncertain = False
    else:
        # Anything short of a clear answer is uncertain rather than silent, so
        # the dashboard can never claim "not mentioned" about a posting that
        # plainly discusses visas.
        soft: list[tuple[str, str]] = []
        if generic:
            soft.append(
                (
                    "work_authorization_required_unspecified",
                    "Work authorization is required, but sponsorship is not addressed.",
                )
            )
        if public_trust:
            soft.append(
                (
                    "public_trust_or_background_check",
                    "A public-trust/background requirement needs manual review.",
                )
            )
        if export_control:
            soft.append(
                (
                    "export_control_mentioned",
                    "Posting mentions export-control rules without stating who is eligible.",
                )
            )
        if clearance_required or _CLEARANCE_MENTIONED.search(non_eeo):
            soft.append(
                (
                    "security_clearance_mentioned",
                    "Posting mentions a security clearance; confirm whether it is required.",
                )
            )
        if _OPT_STATUS.search(non_eeo):
            soft.append(
                (
                    "opt_status_mentioned",
                    "Posting mentions OPT/CPT; read how it treats those candidates.",
                )
            )
        if soft or mentioned:
            status = "uncertain"
            uncertain = True
            for flag, reason in soft:
                flags.append(flag)
                reasons.append(reason)
            if not soft:
                flags.append("sponsorship_language_present")
                reasons.append(
                    "Posting discusses visas or work authorization without stating a "
                    "sponsorship policy; read it before applying."
                )
        else:
            status = "unspecified"
            uncertain = True
            reasons.append("Posting does not state a clear sponsorship policy.")

    return WorkAuthorizationAnalysis(
        status,
        _unique(flags),
        _unique(reasons),
        bool(blocking),
        uncertain,
    )


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_TOKEN = re.compile(rf"\b(?P<month>{_MONTH_PATTERN})\.?\s+(?P<year>20\d{{2}})\b")
_SEASONS = {"winter": 1, "spring": 4, "summer": 7, "fall": 10, "autumn": 10}
_SEASON_TOKEN = re.compile(
    r"\b(?P<season>winter|spring|summer|fall|autumn)\s+(?P<year>20\d{2})\b"
)
_GRAD_CONTEXT = re.compile(
    r"\b(?:graduat(?:e|ed|es|ing|ion)|class\s+of|degree\s+(?:completion|date)|"
    r"expected\s+(?:degree|completion)|complet(?:e|ed|ing|ion)\s+(?:a\s+|the\s+)?degree|"
    r"degree\s+by)\b"
)


def detect_graduation_window(
    text: str | None,
    candidate_year: int = 2026,
    candidate_month: int = 5,
) -> GraduationAnalysis:
    """Compare explicit graduation requirements with the candidate's May 2026 date."""

    source = _clean(text)
    relevant: list[str] = []
    for match in _GRAD_CONTEXT.finditer(source):
        relevant.append(source[max(0, match.start() - 100) : min(len(source), match.end() + 180)])
    grad_text = " ".join(relevant)
    if not grad_text:
        return GraduationAnalysis(
            "unspecified", None, (), (), ("No graduation window stated.",), True
        )

    years = tuple(sorted({int(value) for value in re.findall(r"\b20\d{2}\b", grad_text)}))
    flags: list[str] = []
    reasons: list[str] = []
    candidate = (candidate_year, candidate_month)

    candidate_season = (
        "winter"
        if candidate_month <= 2 or candidate_month == 12
        else "spring"
        if candidate_month <= 5
        else "summer"
        if candidate_month <= 8
        else "fall"
    )
    season_list = re.search(
        r"\b(?P<first>winter|spring|summer|fall|autumn)\s*"
        r"(?:or|and|/)\s*(?P<second>winter|spring|summer|fall|autumn)\s+"
        r"(?P<year>20\d{2})\b",
        grad_text,
    )
    if season_list:
        accepted = {season_list.group("first"), season_list.group("second")}
        if "autumn" in accepted:
            accepted.add("fall")
        compatible = candidate_year == int(season_list.group("year")) and (
            candidate_season in accepted
        )
        flags.append(
            "graduation_window_compatible" if compatible else "graduation_window_incompatible"
        )
        reasons.append(
            "Candidate graduation falls within the stated seasonal cohort."
            if compatible
            else "Candidate graduation falls outside the stated seasonal cohort."
        )
        return GraduationAnalysis(
            "compatible" if compatible else "incompatible",
            compatible,
            years,
            tuple(flags),
            tuple(reasons),
            False,
        )

    season_tokens = [
        (int(match.group("year")), _SEASONS[match.group("season")], match.group("season"))
        for match in _SEASON_TOKEN.finditer(grad_text)
    ]
    if len(season_tokens) >= 2 and re.search(
        r"\b(?:between|from|through|to|and)\b", grad_text
    ):
        start = season_tokens[0][:2]
        end = season_tokens[1][:2]
        if start > end:
            start, end = end, start
        compatible = start <= candidate <= end
        flags.append(
            "graduation_window_compatible" if compatible else "graduation_window_incompatible"
        )
        reasons.append(
            "Candidate graduation falls within the stated seasonal window."
            if compatible
            else "Candidate graduation falls outside the stated seasonal window."
        )
        return GraduationAnalysis(
            "compatible" if compatible else "incompatible",
            compatible,
            years,
            tuple(flags),
            tuple(reasons),
            False,
        )
    if len(season_tokens) == 1:
        required_year, _, required_season = season_tokens[0]
        if required_season == "autumn":
            required_season = "fall"
        compatible = candidate_year == required_year and candidate_season == required_season
        flags.append("graduation_date_compatible" if compatible else "graduation_date_incompatible")
        reasons.append(
            "Candidate graduation matches the stated seasonal cohort."
            if compatible
            else "Posting requires a different seasonal graduation cohort."
        )
        return GraduationAnalysis(
            "compatible" if compatible else "incompatible",
            compatible,
            years,
            tuple(flags),
            tuple(reasons),
            False,
        )

    # Explicit dated ranges provide the strongest evidence.
    tokens = [
        (int(match.group("year")), _MONTHS[match.group("month")])
        for match in _DATE_TOKEN.finditer(grad_text)
    ]
    if len(tokens) >= 2 and re.search(r"\b(?:between|from|through|to|and)\b", grad_text):
        start, end = tokens[0], tokens[1]
        if start > end:
            start, end = end, start
        compatible = start <= candidate <= end
        flags.append(
            "graduation_window_compatible" if compatible else "graduation_window_incompatible"
        )
        reasons.append(
            "Candidate graduation falls within the stated graduation window."
            if compatible
            else "Candidate graduation falls outside the stated graduation window."
        )
        return GraduationAnalysis(
            "compatible" if compatible else "incompatible",
            compatible,
            years,
            tuple(flags),
            tuple(reasons),
            False,
        )

    if len(tokens) == 1 and re.search(
        r"\b(?:expected|must|should|graduat\w*|class of|degree|complet\w*)\b",
        grad_text,
    ):
        required = tokens[0]
        if re.search(r"\b(?:by|no later than|on or before|before)\b", grad_text):
            compatible = candidate <= required
        elif re.search(r"\b(?:no earlier than|on or after)\b", grad_text):
            compatible = candidate >= required
        elif re.search(r"\bafter\b", grad_text):
            compatible = candidate > required
        else:
            compatible = candidate == required
        flags.append("graduation_date_compatible" if compatible else "graduation_date_incompatible")
        reasons.append(
            "Candidate graduation is compatible with the stated date."
            if compatible
            else "Posting requires a different graduation date."
        )
        return GraduationAnalysis(
            "compatible" if compatible else "incompatible",
            compatible,
            years,
            tuple(flags),
            tuple(reasons),
            False,
        )

    seasonal_2026 = bool(
        re.search(r"\b(?:spring|summer)(?:\s+or\s+(?:spring|summer))?\s+2026\b", grad_text)
    )
    if candidate_year in years or (candidate_year == 2026 and seasonal_2026):
        flags.append("graduation_year_compatible")
        reasons.append(f"Stated graduation cohort includes {candidate_year}.")
        return GraduationAnalysis("compatible", True, years, tuple(flags), tuple(reasons), False)
    if years and all(year > candidate_year for year in years):
        flags.append("future_graduation_cohort_only")
        reasons.append("Posting is limited to a later graduation cohort.")
        return GraduationAnalysis("incompatible", False, years, tuple(flags), tuple(reasons), False)
    if years and all(year < candidate_year for year in years):
        flags.append("earlier_graduation_cohort_only")
        reasons.append("Posting appears limited to an earlier graduation cohort.")
        return GraduationAnalysis("incompatible", False, years, tuple(flags), tuple(reasons), False)
    reasons.append("Graduation wording is present but its compatibility is ambiguous.")
    return GraduationAnalysis("uncertain", None, years, (), tuple(reasons), True)


graduation_window = detect_graduation_window


# Titles routinely carry the cohort year on their own — "Software Engineer
# (2027 University Grad)", "New Grad 2026".  Titles are short and unambiguous,
# so a year found beside a graduate token there is trustworthy in a way that a
# year found loose in a long description body is not.
_TITLE_COHORT = re.compile(
    r"\b(?P<leading>20\d{2})\s+(?:new\s+|university\s+|college\s+)?grad(?:uate)?s?\b"
    r"|\b(?:new|university|college)\s+grad(?:uate)?s?\s*[,\-]?\s+(?P<trailing>20\d{2})\b"
    r"|\bclass\s+of\s+(?P<class_of>20\d{2})\b"
)


def detect_title_cohort(
    title: str | None, candidate_year: int = 2026
) -> GraduationAnalysis | None:
    """Read a graduation cohort stated in the job title, if any."""

    match = _TITLE_COHORT.search(_clean(title))
    if match is None:
        return None
    year = int(
        match.group("leading") or match.group("trailing") or match.group("class_of")
    )
    compatible = year == candidate_year
    if compatible:
        return GraduationAnalysis(
            "compatible",
            True,
            (year,),
            ("graduation_year_compatible",),
            (f"Title names the {year} graduating cohort.",),
            False,
        )
    flag = (
        "future_graduation_cohort_only"
        if year > candidate_year
        else "earlier_graduation_cohort_only"
    )
    return GraduationAnalysis(
        "incompatible",
        False,
        (year,),
        (flag,),
        (f"Title limits the role to the {year} graduating cohort.",),
        False,
    )


_RETURN_TO_SCHOOL = re.compile(
    r"\b(?:return(?:ing)?\s+to|continue(?:ing)?\s+(?:at|in))\s+"
    r"(?:school|college|university|a degree program)\s+(?:after|following)\b|"
    r"\bat least one\s+(?:semester|quarter|term)\s+(?:remaining|left)\s+(?:after|following)\b"
)
_ENROLLED = re.compile(
    r"\b(?:must be|required to be|currently)\s+(?:currently\s+)?enrolled\b|"
    r"\bcurrent(?:ly enrolled)?\s+(?:undergraduate|graduate|college|university)\s+student\b|"
    r"\bcurrently pursuing\s+(?:an?\s+)?(?:bachelor|master|phd|degree)"
)


def detect_student_requirement(text: str | None) -> StudentStatusAnalysis:
    source = _clean(text)
    return_required = bool(_RETURN_TO_SCHOOL.search(source))
    enrolled_required = bool(_ENROLLED.search(source))
    if return_required:
        return StudentStatusAnalysis(
            "incompatible",
            enrolled_required,
            True,
            ("Posting requires returning to school after the role/internship.",),
            False,
        )
    if enrolled_required:
        return StudentStatusAnalysis(
            "uncertain",
            True,
            False,
            ("Posting requires current enrollment; timing needs manual review.",),
            True,
        )
    return StudentStatusAnalysis("unspecified", False, False, (), True)


student_status = detect_student_requirement


_NYC_NJ = re.compile(
    r"\b(?:new york city|nyc|manhattan|brooklyn|queens|bronx|staten island|"
    r"jersey city|hoboken|newark,?\s+nj|new jersey)\b|\bnew york,\s*ny\b|,\s*nj\b"
)
_US_COUNTRY = re.compile(r"\b(?:united states(?: of america)?|u\.?s\.?a?\.?|us-based)\b")
_REMOTE = re.compile(r"\b(?:remote(?:ly)?|work from home|distributed (?:team|workforce|company))\b")
_REMOTE_US = re.compile(
    r"\b(?:remote(?:ly)?\s*(?:[-,/()]|in|within|across)?\s*"
    r"(?:anywhere\s+in\s+)?(?:the\s+)?(?:u\.?s\.?a?|united states)|"
    r"(?:u\.?s\.?a?|united states)\s*(?:[-,/()]|based)?\s*remote|"
    r"remote\s+anywhere\s+in\s+the\s+united states|must reside in the (?:u\.?s\.?|united states))\b"
)
_REMOTE_GLOBAL = re.compile(
    r"\b(?:remote(?:ly)?\s*(?:[-,/()]|in|within|across)?\s*"
    r"(?:worldwide|globally|anywhere in the world|"
    r"north america|the americas)|(?:worldwide|global)\s+remote)\b"
)
_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    "hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    "michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new mexico|"
    "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    "district of columbia"
)
_US_STATE = re.compile(rf"\b(?:{_STATE_NAMES})\b", re.IGNORECASE)
_US_ABBREVIATION = re.compile(
    r"(?:,\s*|\b(?:location|based in|office in)\s+)(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|"
    r"in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nm|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|"
    r"tx|ut|vt|va|wa|wv|wi|wy|ny|dc)\b"
)
_NON_US = re.compile(
    r"\b(?:canada|mexico|united kingdom|uk|england|ireland|france|germany|spain|italy|"
    r"netherlands|poland|sweden|switzerland|india|singapore|australia|new zealand|brazil|"
    r"argentina|japan|china|hong kong|israel|dubai|london|toronto|vancouver|berlin|paris|"
    r"amsterdam|dublin|sydney|melbourne|bangalore|bengaluru|hyderabad)\b"
)


def classify_location(
    location: str | None,
    workplace_type: str | None = None,
    description: str | None = None,
    secondary_locations: Iterable[str] = (),
) -> LocationAnalysis:
    """Assign the user's location preference tier without hiding other U.S. jobs."""

    location_text = " ".join(
        part for part in (_clean(location), *(_clean(item) for item in secondary_locations)) if part
    )
    context = " ".join(
        part
        for part in (location_text, _clean(workplace_type), _clean(description))
        if part
    )
    remote = bool(_REMOTE.search(context))
    if _NYC_NJ.search(location_text) or location_text == "new york":
        return LocationAnalysis("nyc_nj", 4, True, remote, ("NYC/NJ target location.",), False)
    if remote and (
        _REMOTE_US.search(context)
        or _REMOTE_GLOBAL.search(context)
        or _US_COUNTRY.search(location_text)
    ):
        return LocationAnalysis(
            "remote_us",
            3,
            True,
            True,
            ("Remote work is explicitly available in the U.S.",),
            False,
        )
    if (
        _US_COUNTRY.search(location_text)
        or _US_STATE.search(location_text)
        or _US_ABBREVIATION.search(location_text)
    ):
        return LocationAnalysis(
            "other_us",
            2,
            True,
            remote,
            ("U.S. location outside the primary NYC/NJ tier.",),
            False,
        )
    if _NON_US.search(location_text):
        return LocationAnalysis(
            "non_us",
            0,
            False,
            remote,
            ("Only a non-U.S. location was detected.",),
            False,
        )
    if remote:
        return LocationAnalysis(
            "remote_unspecified",
            1,
            None,
            True,
            ("Remote location is stated, but U.S. availability is not explicit.",),
            True,
        )
    return LocationAnalysis("unknown", 1, None, False, ("Location eligibility is unclear.",), True)


location_eligibility = classify_location


def analyze_eligibility(
    title: str | None,
    description: str | None,
    location: str | None = None,
    workplace_type: str | None = None,
    secondary_locations: Iterable[str] = (),
    candidate_graduation_year: int = 2026,
    candidate_graduation_month: int = 5,
) -> EligibilityAnalysis:
    """Run all eligibility extractors and aggregate explicit blockers."""

    experience = extract_year_requirements(description)
    seniority = detect_seniority(title, description, experience)
    authorization = detect_work_authorization(description)
    graduation = detect_graduation_window(
        description, candidate_graduation_year, candidate_graduation_month
    )
    # A cohort named in the title settles what a vague description leaves open.
    title_cohort = detect_title_cohort(title, candidate_graduation_year)
    if title_cohort is not None and graduation.status in {"unspecified", "uncertain"}:
        graduation = title_cohort
    student = detect_student_requirement(description)
    location_result = classify_location(location, workplace_type, description, secondary_locations)
    levels = detect_level_range(title, description, experience)

    flags: list[str] = [*authorization.flags, *graduation.flags]
    reasons: list[str] = []
    blockers = (
        authorization.explicit_blocker
        or graduation.compatible is False
        or student.status == "incompatible"
    )
    if seniority.likely_senior:
        blockers = True
        flags.append("senior_role")
        reasons.append("Title or required experience indicates a senior-level role.")
    required_minima = [item.min_years for item in experience if item.required]
    if required_minima and max(required_minima) >= 4:
        blockers = True
        flags.append("four_plus_years_required")
        reasons.append("Posting requires at least four years of experience.")
    if location_result.us_eligible is False:
        blockers = True
        flags.append("outside_us")
        reasons.append("No U.S.-compatible location was found.")
    reasons.extend(authorization.reasons if authorization.explicit_blocker else ())
    reasons.extend(graduation.reasons if graduation.compatible is False else ())
    reasons.extend(student.reasons if student.status == "incompatible" else ())
    uncertain = any(
        (
            authorization.uncertain,
            graduation.uncertain,
            student.uncertain and student.status != "unspecified",
            location_result.uncertain,
            seniority.uncertain,
            any(item.requirement == "uncertain" for item in experience),
        )
    )
    return EligibilityAnalysis(
        experience,
        seniority,
        authorization,
        graduation,
        student,
        location_result,
        levels,
        blockers,
        _unique(flags),
        _unique(reasons),
        uncertain,
    )


analyze_job_eligibility = analyze_eligibility


__all__ = [
    "EligibilityAnalysis",
    "ExperienceRequirement",
    "GraduationAnalysis",
    "LevelRangeAnalysis",
    "LocationAnalysis",
    "SeniorityAnalysis",
    "StudentStatusAnalysis",
    "WorkAuthorizationAnalysis",
    "analyze_eligibility",
    "analyze_job_eligibility",
    "classify_location",
    "detect_graduation_window",
    "detect_level_range",
    "detect_seniority",
    "detect_student_requirement",
    "detect_title_cohort",
    "detect_work_authorization",
    "extract_experience_requirements",
    "extract_year_requirements",
    "graduation_window",
    "location_eligibility",
    "student_status",
]
