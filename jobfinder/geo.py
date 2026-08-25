"""U.S.-wide geography tagging for job locations.

Collection is nationwide: nothing is dropped for being outside New York or New
Jersey.  Instead every posting is tagged with the U.S. buckets that apply to it,
and the dashboard lets a reader pick any combination of them.

A posting can carry several tags at once.  A role open in both New York and
Austin is genuinely both ``nyc`` and ``other_us``, and a remote role with a New
Jersey office is both ``remote_us`` and ``new_jersey``.  Collapsing that into one
category is what made earlier versions hide real options.

Raw location text is never rewritten.  These tags sit beside it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import html
import re
from typing import Any, Iterable


NYC = "nyc"
NEW_JERSEY = "new_jersey"
REMOTE_US = "remote_us"
OTHER_US = "other_us"

#: Every tag the dashboard offers, in display order.
GEO_TAGS: tuple[str, ...] = (NYC, NEW_JERSEY, REMOTE_US, OTHER_US)

GEO_TAG_LABELS: dict[str, str] = {
    NYC: "NYC",
    NEW_JERSEY: "New Jersey",
    REMOTE_US: "Remote US",
    OTHER_US: "Other US",
}


def _clean(value: Any) -> str:
    text = html.unescape("" if value is None else str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


_NYC = re.compile(
    r"\bnew york city\b|\bnyc\b|\bmanhattan\b|\bbrooklyn\b|\bqueens\b|\bthe bronx\b|"
    r"\bstaten island\b|\blong island city\b|\bnew york,?\s*(?:ny|new york)\b|"
    r"\bny,?\s*(?:ny|new york)\b"
)
_NEW_JERSEY = re.compile(
    r"\bnew jersey\b|\bjersey city\b|\bhoboken\b|\bnewark,?\s*(?:nj|new jersey)\b|"
    r"\bprinceton,?\s*(?:nj|new jersey)\b|\bweehawken\b|\bsecaucus\b|"
    r"\bedison,?\s*(?:nj|new jersey)\b|,\s*nj\b|\bnj\b"
)
# "New York" with nothing after it is the city far more often than the state on
# a job board, so it is treated as NYC rather than being thrown away.
_BARE_NEW_YORK = re.compile(r"^\s*(?:greater\s+)?new york(?:\s+area|\s+metro(?:politan)?(?:\s+area)?)?\s*$")

_US_COUNTRY = re.compile(
    r"\bunited states(?: of america)?\b|\bu\.s\.a?\.?\b|\busa\b|(?<![a-z])us(?![a-z])|"
    r"\bus[- ]based\b|\bstateside\b"
)
_STATE_NAMES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    "hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    "michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new mexico|"
    "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    "district of columbia|puerto rico"
)
_US_STATE = re.compile(rf"\b(?:{_STATE_NAMES})\b")
# A two-letter state code is only trusted next to a separator or an explicit
# location word, so "in" (Indiana) does not fire on ordinary prose.
_US_ABBREVIATION = re.compile(
    r"(?:,\s*|-\s*|\b(?:location|based in|office in|remote in|hq)\s+)"
    r"(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|"
    r"ne|nv|nh|nm|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|ny|nj|dc)"
    r"(?![a-z])"
)
# Cities big enough to appear on their own with no state or country beside them.
# Only used as a fallback: Ashby's structured address supplies the country for
# roughly 97% of live postings.
_US_CITY = re.compile(
    r"\b(?:san francisco|sf bay area|(?<![a-z])sf(?![a-z])|palo alto|mountain view|menlo park|"
    r"sunnyvale|santa clara|san jose|san mateo|redwood city|foster city|san carlos|cupertino|"
    r"oakland|berkeley|los angeles|santa monica|pasadena|irvine|san diego|sacramento|"
    r"seattle|bellevue|redmond|portland|denver|boulder|austin|dallas|houston|san antonio|"
    r"chicago|boston|cambridge, ma|somerville|atlanta|miami|orlando|tampa|nashville|"
    r"charlotte|raleigh|durham|pittsburgh|philadelphia|baltimore|washington,? d\.?c\.?|"
    r"minneapolis|detroit|salt lake city|phoenix|scottsdale|tempe|las vegas|kansas city|"
    r"st\.? louis|columbus|cincinnati|cleveland|indianapolis|madison, wi|ann arbor|"
    r"provo|boise|honolulu|anchorage|el segundo|culver city|playa vista|torrance|"
    r"huntington beach|costa mesa|long beach|fremont|milpitas|pleasanton|walnut creek)\b"
)
_NON_US = re.compile(
    r"\b(?:canada|toronto|vancouver|montreal|ottawa|calgary|waterloo, on|"
    r"mexico|mexico city|guadalajara|"
    r"united kingdom|england|scotland|wales|london|manchester|edinburgh|cambridge, uk|"
    r"ireland|dublin|france|paris|germany|berlin|munich|hamburg|spain|madrid|barcelona|"
    r"italy|milan|rome|netherlands|amsterdam|utrecht|belgium|brussels|portugal|lisbon|porto|"
    r"poland|warsaw|krakow|kraków|wroclaw|romania|bucharest|cluj|czech|prague|austria|vienna|"
    r"switzerland|zurich|zürich|geneva|sweden|stockholm|norway|oslo|denmark|copenhagen|"
    r"finland|helsinki|estonia|tallinn|lithuania|vilnius|latvia|riga|ukraine|kyiv|kiev|"
    r"greece|athens|turkey|istanbul|israel|tel aviv|jerusalem|united arab emirates|dubai|"
    r"abu dhabi|saudi arabia|riyadh|egypt|cairo|nigeria|lagos|kenya|nairobi|south africa|"
    r"cape town|johannesburg|india|bangalore|bengaluru|hyderabad|mumbai|delhi|gurgaon|"
    r"gurugram|pune|chennai|noida|singapore|malaysia|kuala lumpur|indonesia|jakarta|"
    r"philippines|manila|vietnam|hanoi|ho chi minh|thailand|bangkok|japan|tokyo|osaka|"
    r"korea|seoul|china|beijing|shanghai|shenzhen|hong kong|taiwan|taipei|"
    r"australia|sydney|melbourne|brisbane|new zealand|auckland|wellington|"
    r"brazil|sao paulo|são paulo|rio de janeiro|argentina|buenos aires|chile|santiago|"
    r"colombia|bogota|bogotá|peru|lima|uruguay|montevideo|costa rica|san jose, costa rica)\b"
)
_REMOTE = re.compile(
    r"\bremote(?:ly)?\b|\bwork from home\b|\bwfh\b|\bdistributed (?:team|workforce|company)\b|"
    r"\banywhere\b|\bfully distributed\b"
)
# Remote scoped to somewhere that is not the U.S. ("Remote - EMEA").
_REMOTE_NON_US = re.compile(
    r"\bremote\s*(?:[-,/(]|\s)\s*(?:emea|apac|latam|europe|european union|eu\b|uk\b|india|"
    r"canada only|anz)\b"
)


def _split_places(
    location: Any,
    secondary_locations: Iterable[Any] = (),
    location_context: Any = "",
) -> list[str]:
    """Return each place a posting names as its own string.

    Keeping them separate is what allows a two-city posting to earn two tags.
    ``location_context`` is Ashby's structured address block, already flattened
    to ``locality, region, country`` fragments joined by ``;``.
    """

    places: list[str] = []
    primary = _clean(location)
    if primary:
        places.append(primary)
    for item in secondary_locations or ():
        value = _clean(item)
        if value:
            places.append(value)
    for fragment in str(location_context or "").split(";"):
        value = _clean(fragment)
        if value:
            places.append(value)
    return list(dict.fromkeys(places))


def _us_signal(place: str) -> bool:
    """True when this one place string points at the United States."""

    if _NON_US.search(place):
        return False
    return bool(
        _US_COUNTRY.search(place)
        or _US_STATE.search(place)
        or _US_ABBREVIATION.search(place)
        or _US_CITY.search(place)
    )


@dataclass(frozen=True, slots=True)
class GeoAnalysis:
    """Where a posting can actually be worked from, as filterable tags."""

    tags: tuple[str, ...]
    us_based: bool | None  # None means the posting never says
    remote: bool
    raw_locations: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def in_scope(self) -> bool:
        """True when at least one U.S. tag applies."""

        return bool(self.tags)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(GEO_TAG_LABELS[tag] for tag in self.tags)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze_geography(
    location: Any = None,
    workplace_type: Any = None,
    secondary_locations: Iterable[Any] = (),
    location_context: Any = "",
    description: Any = None,
) -> GeoAnalysis:
    """Tag one posting with every U.S. geography bucket that applies to it."""

    places = _split_places(location, secondary_locations, location_context)
    workplace = _clean(workplace_type)
    joined = " ; ".join(places)
    remote = bool(_REMOTE.search(joined) or _REMOTE.search(workplace))

    tags: list[str] = []
    reasons: list[str] = []
    any_us = False
    any_non_us = False

    for place in places:
        if _NON_US.search(place):
            any_non_us = True
            continue
        is_nyc = bool(_NYC.search(place) or _BARE_NEW_YORK.match(place))
        is_nj = bool(_NEW_JERSEY.search(place))
        us_here = is_nyc or is_nj or _us_signal(place)
        if not us_here:
            continue
        any_us = True
        if is_nyc and NYC not in tags:
            tags.append(NYC)
            reasons.append(f"New York City location: {place!r}.")
        if is_nj and NEW_JERSEY not in tags:
            tags.append(NEW_JERSEY)
            reasons.append(f"New Jersey location: {place!r}.")
        if not is_nyc and not is_nj and OTHER_US not in tags:
            tags.append(OTHER_US)
            reasons.append(f"U.S. location outside NYC/NJ: {place!r}.")

    # Remote is a separate axis: a role can be remote *and* tied to a city.
    if remote and not _REMOTE_NON_US.search(joined):
        if any_us or _US_COUNTRY.search(joined) or _US_STATE.search(joined):
            if REMOTE_US not in tags:
                tags.append(REMOTE_US)
                reasons.append("Remote work is available from the United States.")
            any_us = True
        elif not any_non_us:
            # "Remote" with no geography at all.  Neither claim it for the U.S.
            # nor discard it; the dashboard shows it as unknown geography.
            reasons.append("Remote posting does not say which countries are eligible.")

    if not tags and any_non_us:
        reasons.append("Only non-U.S. locations were found.")
    elif not tags and not places:
        reasons.append("Posting lists no location at all.")
    elif not tags:
        reasons.append("Location text did not identify a country.")

    ordered = tuple(tag for tag in GEO_TAGS if tag in tags)
    if any_us:
        us_based: bool | None = True
    elif any_non_us:
        us_based = False
    else:
        us_based = None
    return GeoAnalysis(ordered, us_based, remote, tuple(places), tuple(reasons))


def geo_tags(
    location: Any = None,
    workplace_type: Any = None,
    secondary_locations: Iterable[Any] = (),
    location_context: Any = "",
    description: Any = None,
) -> tuple[str, ...]:
    """Convenience wrapper returning just the tags."""

    return analyze_geography(
        location, workplace_type, secondary_locations, location_context, description
    ).tags


def geography_from_job(job: Any) -> GeoAnalysis:
    """Tag a ``NormalizedJob`` or any object exposing the same attributes."""

    return analyze_geography(
        getattr(job, "location", None),
        getattr(job, "workplace_type", None),
        getattr(job, "secondary_locations", ()) or (),
        getattr(job, "location_context", "") or "",
        getattr(job, "description_text", None),
    )


def label_for(tag: str) -> str:
    return GEO_TAG_LABELS.get(tag, tag.replace("_", " ").title())


__all__ = [
    "GEO_TAGS",
    "GEO_TAG_LABELS",
    "GeoAnalysis",
    "NEW_JERSEY",
    "NYC",
    "OTHER_US",
    "REMOTE_US",
    "analyze_geography",
    "geo_tags",
    "geography_from_job",
    "label_for",
]
