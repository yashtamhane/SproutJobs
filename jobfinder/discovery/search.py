"""Optional discovery through a search endpoint the user is allowed to query.

This is **off by default and ships with no endpoints configured**, on purpose.
Most large search engines forbid automated querying in their terms of service,
and this project does not work around that — no key-free scraping of a service
that has said no, no rotating user agents, no CAPTCHA handling.

What it does support is the case where a reader genuinely has an endpoint they
may query: a self-hosted SearXNG instance, an institutional search API, or any
service whose ``robots.txt`` and terms permit it.  Point
``discovery.search_endpoints`` at URL templates containing ``{query}`` and the
results are mined for ``jobs.ashbyhq.com`` links like any other public page.

``robots.txt`` is checked before every request, and a disallowed endpoint is
skipped and reported rather than retried by another route.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import quote_plus

import requests

from .base import DiscoveryCandidate, DiscoveryResult
from .cache import SlugCache
from .robots import RobotsPolicy
from .urls import extract_ashby_slugs

LOGGER = logging.getLogger(__name__)

#: Queries that surface board links without asking a service to enumerate them.
DEFAULT_QUERIES: tuple[str, ...] = (
    'site:jobs.ashbyhq.com "software engineer"',
    'site:jobs.ashbyhq.com "new grad"',
    'site:jobs.ashbyhq.com "entry level"',
)


class SearchEngineDiscoverer:
    """Mine Ashby links out of a permitted search endpoint's results."""

    name = "search_engine"

    def __init__(
        self,
        endpoints: Sequence[str],
        cache_directory: Path | str,
        *,
        user_agent: str,
        queries: Sequence[str] = DEFAULT_QUERIES,
        timeout: float = 20.0,
        refresh_hours: int = 24,
        session: Optional[requests.Session] = None,
        robots: Optional[RobotsPolicy] = None,
    ) -> None:
        self.endpoints = [value for value in endpoints if value]
        self.queries = list(queries)
        self.user_agent = user_agent
        self.timeout = timeout
        self.session = session or requests.Session()
        self.cache = SlugCache(cache_directory, refresh_hours=refresh_hours)
        self.robots = robots

    def _request(self, url: str) -> tuple[List[str], Optional[str]]:
        if self.robots is not None and not self.robots.allows(url):
            return [], f"{url}: disallowed by robots.txt"
        try:
            response = self.session.get(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/json,text/plain",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return [], f"{url}: {exc}"
        return extract_ashby_slugs(response.text, strict=True), None

    def discover(self, force: bool = False) -> DiscoveryResult:
        result = DiscoveryResult(source=self.name)
        if not self.endpoints:
            return result
        cache_key = "search:" + "|".join(self.endpoints) + "|" + "|".join(self.queries)
        if not force:
            cached = self.cache.load(cache_key)
            if cached is not None:
                result.fetched = True
                result.from_cache = True
                result.documents_read = 1
                result.candidates = [
                    DiscoveryCandidate(slug=slug, source=self.name, evidence="cache")
                    for slug in cached
                ]
                return result

        errors: List[str] = []
        found: List[str] = []
        seen: set[str] = set()
        for endpoint in self.endpoints:
            for query in self.queries:
                url = (
                    endpoint.replace("{query}", quote_plus(query))
                    if "{query}" in endpoint
                    else endpoint
                )
                slugs, error = self._request(url)
                if error:
                    errors.append(error)
                    result.documents_failed += 1
                    continue
                result.documents_read += 1
                result.fetched = True
                for slug in slugs:
                    key = slug.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(slug)
                    result.candidates.append(
                        DiscoveryCandidate(slug=slug, source=self.name, evidence=url)
                    )
        if found:
            self.cache.save(cache_key, found)
        result.error = "; ".join(errors) if errors else None
        return result


__all__ = ["DEFAULT_QUERIES", "SearchEngineDiscoverer"]
