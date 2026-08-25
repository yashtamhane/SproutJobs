"""Strict extraction of Ashby board slugs from public URLs and documents."""

from __future__ import annotations

import html
import re
from typing import List, Optional
from urllib.parse import unquote, urlsplit


_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_URL_RE = re.compile(
    r"(?:https?://)?jobs\.ashbyhq\.com(?:/|%2[fF])([A-Za-z0-9][A-Za-z0-9._-]{0,99})",
    re.IGNORECASE,
)
_RESERVED = {"api", "favicon.ico", "robots.txt", "sitemap.xml", "meeting", "b"}

# Web archives capture Ashby's own client-side state and telemetry URLs, which
# look like board paths but never are.  ``root.<uuid>`` is by far the most
# common: a single crawl contributed two thousand of them.  Filtering these
# here means the verification stage is not spent on guaranteed 404s.
_INTERNAL_PATH = re.compile(
    r"^(?:root|application|posting|jobs?-page)[._-][0-9a-f]{8}[_-][0-9a-f]{4}", re.IGNORECASE
)
_BARE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# Ashby renders a marketing page at a handful of first-level paths.
_NON_BOARD_PATHS = {
    "assets",
    "cdn",
    "embed",
    "static",
    "_next",
    "favicon",
    "images",
    "img",
    "js",
    "css",
    "fonts",
}


def validate_slug(slug: str) -> bool:
    normalized = (slug or "").strip()
    return bool(_SLUG_RE.fullmatch(normalized)) and normalized.lower() not in _RESERVED


def is_probable_board_slug(slug: str) -> bool:
    """Reject slug shapes that are known never to be job boards.

    This is a cheap filter on obvious noise, not a guess about whether a board
    exists.  Anything that survives is still verified against Ashby's API.
    """

    if not validate_slug(slug):
        return False
    normalized = slug.strip().lower()
    if normalized in _NON_BOARD_PATHS:
        return False
    if _INTERNAL_PATH.match(normalized) or _BARE_UUID.match(normalized):
        return False
    return True


def extract_ashby_slug(value: str) -> Optional[str]:
    """Return the first path segment only for a genuine Ashby board URL."""

    if not value:
        return None
    decoded = html.unescape(value.strip())
    for _ in range(2):
        decoded = unquote(decoded)
    candidate_url = decoded if "://" in decoded else "https://" + decoded.lstrip("/")
    try:
        parsed = urlsplit(candidate_url)
    except ValueError:
        return None
    if (parsed.hostname or "").rstrip(".").lower() != "jobs.ashbyhq.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or not validate_slug(parts[0]):
        return None
    return parts[0]


def extract_ashby_slugs(text: str, *, strict: bool = False) -> List[str]:
    """Extract unique slugs in first-seen order, including percent-encoded URLs.

    ``strict`` additionally drops the internal-path shapes that web archives
    are full of; it is used by the archive discoverers.
    """

    if not text:
        return []
    decoded = html.unescape(text)
    for _ in range(2):
        decoded = unquote(decoded)
    found = []
    seen = set()
    accept = is_probable_board_slug if strict else validate_slug
    for match in _URL_RE.finditer(decoded):
        slug = match.group(1)
        key = slug.lower()
        if accept(slug) and key not in seen:
            seen.add(key)
            found.append(slug)
    return found


__all__ = [
    "extract_ashby_slug",
    "extract_ashby_slugs",
    "is_probable_board_slug",
    "validate_slug",
]
