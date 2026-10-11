"""Discovery from legitimately public source documents.

The configured defaults are public open-source job-list repositories and public
pages that already publish Ashby board links.  Every slug produced here remains
an untrusted candidate until the Ashby endpoint is queried successfully by the
pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import requests

from .base import DiscoveryCandidate, DiscoveryResult
from .cache import SlugCache
from .robots import RobotsPolicy
from .urls import extract_ashby_slugs

LOGGER = logging.getLogger(__name__)


class PublicDatasetDiscoverer:
    """Harvest Ashby board slugs from public documents that already list them.

    Only the extracted slugs are cached.  Some of these sources are several
    megabytes of JSON, and keeping the derived result rather than the raw body
    keeps the local cache small while still preventing needless refetching.
    """

    name = "public_dataset"
    # Every discovery fetch asks robots.txt first, repository raw-content hosts
    # included.  Those hosts publish no policy today, so this changes nothing
    # in practice — but "we decided this host does not need checking" is not a
    # posture a public client should ship with, and the day one of them does
    # publish a policy, this honors it without anyone noticing.
    check_robots = True

    def __init__(
        self,
        urls: Sequence[str],
        cache_directory: Path | str,
        *,
        user_agent: str,
        timeout: float = 20.0,
        refresh_hours: int = 24,
        session: Optional[requests.Session] = None,
        robots: Optional[RobotsPolicy] = None,
    ) -> None:
        self.urls = list(urls)
        self.cache_directory = Path(cache_directory)
        self.user_agent = user_agent
        self.timeout = timeout
        self.cache = SlugCache(cache_directory, refresh_hours=refresh_hours)
        self.session = session or requests.Session()
        self.robots = robots

    def _fetch_one(self, url: str, force: bool) -> Tuple[Optional[List[str]], bool, Optional[str]]:
        """Return ``(slugs, from_cache, error)`` without ever raising."""

        if not force:
            cached = self.cache.load(url)
            if cached is not None:
                return cached, True, None
        if self.check_robots and self.robots is not None and not self.robots.allows(url):
            LOGGER.info("Skipping %s: disallowed by robots.txt", url)
            return None, False, f"{url}: disallowed by robots.txt"
        try:
            response = self.session.get(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/plain,application/json,text/markdown,text/html",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            slugs = extract_ashby_slugs(response.text, strict=True)
            self.cache.save(url, slugs)
            return slugs, False, None
        except requests.RequestException as exc:
            stale = self.cache.load(url, allow_stale=True)
            if stale is not None:
                LOGGER.warning("Discovery source failed; using cached slugs: %s", url)
                return stale, True, str(exc)
            return None, False, str(exc)

    def discover(self, force: bool = False) -> DiscoveryResult:
        result = DiscoveryResult(source=self.name)
        seen = set()
        errors = []
        cache_hits = 0
        for url in self.urls:
            slugs, from_cache, error = self._fetch_one(url, force)
            cache_hits += int(from_cache)
            if error:
                errors.append("{}: {}".format(url, error))
            if slugs is None:
                result.documents_failed += 1
                continue
            result.fetched = True
            result.documents_read += 1
            for slug in slugs:
                key = slug.lower()
                if key in seen:
                    continue
                seen.add(key)
                result.candidates.append(
                    DiscoveryCandidate(slug=slug, source=self.name, evidence=url)
                )
        result.from_cache = bool(self.urls) and cache_hits == len(self.urls)
        result.error = "; ".join(errors) if errors else None
        return result


class PublicPageDiscoverer(PublicDatasetDiscoverer):
    """Same safe harvester for user-configured public HTML pages.

    Identical behavior to the parent; it exists as a separate provider so a
    page you added yourself is reported and cached under its own name rather
    than blending into the curated dataset list.
    """

    name = "public_page"
    check_robots = True


__all__ = ["PublicDatasetDiscoverer", "PublicPageDiscoverer"]
