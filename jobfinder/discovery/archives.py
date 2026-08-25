"""Discovery from free public web-archive indexes.

Two organizations publish, at no cost and with no account, a queryable index of
URLs they have crawled: the Common Crawl Foundation and the Internet Archive.
Both expose a CDX-style API intended for exactly this kind of programmatic
lookup, and both answer the one question this project needs — *which paths under
``jobs.ashbyhq.com`` have ever existed?*

This is the opposite of guessing at slugs.  Nothing is enumerated or brute
forced: a third party observed these URLs on the public web, and every slug they
report is still verified against Ashby's own API before it is trusted.  A slug
that no longer resolves simply fails verification and is dropped.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

import requests

from .base import DiscoveryCandidate, DiscoveryResult
from .cache import SlugCache
from .robots import RobotsPolicy
from .urls import is_probable_board_slug

LOGGER = logging.getLogger(__name__)

ASHBY_HOST = "jobs.ashbyhq.com"
COMMON_CRAWL_COLLECTIONS = "https://index.commoncrawl.org/collinfo.json"
COMMON_CRAWL_INDEX = "https://index.commoncrawl.org/{index}-index"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"


def _slug_from_url(url: str) -> Optional[str]:
    """Take the first path segment of an archived ``jobs.ashbyhq.com`` URL."""

    try:
        parts = urlsplit(url if "://" in url else "http://" + url)
    except ValueError:
        return None
    if (parts.hostname or "").rstrip(".").lower() != ASHBY_HOST:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return None
    slug = segments[0]
    return slug if is_probable_board_slug(slug) else None


def _collect(urls: Iterable[str]) -> List[str]:
    """Reduce a stream of archived URLs to unique slugs in first-seen order."""

    found: List[str] = []
    seen: set[str] = set()
    for url in urls:
        slug = _slug_from_url(url)
        if slug is None:
            continue
        key = slug.casefold()
        if key not in seen:
            seen.add(key)
            found.append(slug)
    return found


class _ArchiveDiscoverer:
    """Shared caching, politeness, and result assembly for archive sources."""

    name = "archive"

    def __init__(
        self,
        cache_directory,
        *,
        user_agent: str,
        timeout: float = 60.0,
        refresh_hours: int = 24,
        session: Optional[requests.Session] = None,
        robots: Optional[RobotsPolicy] = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.session = session or requests.Session()
        self.cache = SlugCache(cache_directory, refresh_hours=refresh_hours)
        self.robots = robots

    def _allowed(self, url: str) -> bool:
        return self.robots is None or self.robots.allows(url)

    def _get(self, url: str, params: dict | None = None, *, stream: bool = True):
        return self.session.get(
            url,
            params=params,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=self.timeout,
            stream=stream,
        )

    def _fetch_slugs(self) -> tuple[List[str], List[str]]:
        """Return ``(slugs, errors)``.  Subclasses implement the query."""

        raise NotImplementedError

    def discover(self, force: bool = False) -> DiscoveryResult:
        result = DiscoveryResult(source=self.name)
        cache_key = self.cache_key()
        if not force:
            cached = self.cache.load(cache_key)
            if cached is not None:
                result.fetched = True
                result.from_cache = True
                result.documents_read = 1
                result.candidates = [
                    DiscoveryCandidate(slug=slug, source=self.name, evidence=self.evidence())
                    for slug in cached
                ]
                return result
        try:
            slugs, errors = self._fetch_slugs()
        except requests.RequestException as exc:
            stale = self.cache.load(cache_key, allow_stale=True)
            if stale is not None:
                LOGGER.warning("%s unreachable; using cached slugs: %s", self.name, exc)
                result.fetched = True
                result.from_cache = True
                result.error = str(exc)
                result.candidates = [
                    DiscoveryCandidate(slug=slug, source=self.name, evidence=self.evidence())
                    for slug in stale
                ]
                return result
            result.error = str(exc)
            result.documents_failed = 1
            return result
        if slugs:
            self.cache.save(cache_key, slugs)
        result.fetched = True
        result.documents_read = 1
        result.documents_failed = len(errors)
        result.error = "; ".join(errors) if errors else None
        result.candidates = [
            DiscoveryCandidate(slug=slug, source=self.name, evidence=self.evidence())
            for slug in slugs
        ]
        return result

    def cache_key(self) -> str:
        return self.name

    def evidence(self) -> str:
        return self.name


class CommonCrawlDiscoverer(_ArchiveDiscoverer):
    """Query Common Crawl's public CDX index for archived Ashby board paths.

    Common Crawl publishes a new index roughly monthly.  Reading more than one
    widens coverage, because a board that was quiet during one crawl is often
    present in another.
    """

    name = "common_crawl"

    def __init__(
        self,
        cache_directory,
        *,
        user_agent: str,
        indexes: int = 2,
        max_pages: int = 6,
        timeout: float = 90.0,
        refresh_hours: int = 168,
        session: Optional[requests.Session] = None,
        robots: Optional[RobotsPolicy] = None,
    ) -> None:
        super().__init__(
            cache_directory,
            user_agent=user_agent,
            timeout=timeout,
            refresh_hours=refresh_hours,
            session=session,
            robots=robots,
        )
        self.indexes = max(1, int(indexes))
        self.max_pages = max(1, int(max_pages))

    def cache_key(self) -> str:
        return f"{self.name}-{self.indexes}"

    def evidence(self) -> str:
        return COMMON_CRAWL_COLLECTIONS

    def _recent_indexes(self) -> List[str]:
        if not self._allowed(COMMON_CRAWL_COLLECTIONS):
            raise PermissionError(
                "index.commoncrawl.org/robots.txt disallows the collection list"
            )
        response = self._get(COMMON_CRAWL_COLLECTIONS, stream=False)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        identifiers = [
            str(entry["id"])
            for entry in payload
            if isinstance(entry, dict) and entry.get("id")
        ]
        return identifiers[: self.indexes]

    def _query_index(self, index: str) -> tuple[List[str], Optional[str]]:
        url = COMMON_CRAWL_INDEX.format(index=index)
        if not self._allowed(url):
            return [], f"{index}: disallowed by robots.txt"
        urls: List[str] = []
        for page in range(self.max_pages):
            response = self._get(
                url,
                {
                    "url": f"{ASHBY_HOST}/*",
                    "output": "json",
                    "fl": "url",
                    "page": page,
                },
            )
            if response.status_code == 404:
                # The API reports "no more pages" with a 404 body.
                break
            response.raise_for_status()
            lines = 0
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                lines += 1
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                value = record.get("url") if isinstance(record, dict) else None
                if value:
                    urls.append(str(value))
            if not lines:
                break
        return urls, None

    def _fetch_slugs(self) -> tuple[List[str], List[str]]:
        errors: List[str] = []
        try:
            indexes = self._recent_indexes()
        except PermissionError as exc:
            # A refusal is reported plainly and never routed around.
            LOGGER.info("Skipping Common Crawl: %s", exc)
            return [], [f"skipped: {exc}"]
        except (requests.RequestException, ValueError) as exc:
            return [], [f"collection list: {exc}"]
        collected: List[str] = []
        for index in indexes:
            try:
                urls, error = self._query_index(index)
            except requests.RequestException as exc:
                errors.append(f"{index}: {exc}")
                continue
            if error:
                errors.append(error)
            collected.extend(urls)
            LOGGER.debug("Common Crawl %s returned %s archived URLs", index, len(urls))
        return _collect(collected), errors


class WaybackDiscoverer(_ArchiveDiscoverer):
    """Query the Internet Archive's public CDX index for Ashby board paths.

    The Wayback index reaches much further back than any single Common Crawl
    snapshot, so it surfaces boards that were live at any point.  Many of those
    companies have since closed their board; verification removes them.
    """

    name = "wayback"

    def __init__(
        self,
        cache_directory,
        *,
        user_agent: str,
        limit: int = 200_000,
        timeout: float = 180.0,
        refresh_hours: int = 168,
        session: Optional[requests.Session] = None,
        robots: Optional[RobotsPolicy] = None,
    ) -> None:
        super().__init__(
            cache_directory,
            user_agent=user_agent,
            timeout=timeout,
            refresh_hours=refresh_hours,
            session=session,
            robots=robots,
        )
        self.limit = max(1000, int(limit))

    def evidence(self) -> str:
        return WAYBACK_CDX

    def _fetch_slugs(self) -> tuple[List[str], List[str]]:
        if not self._allowed(WAYBACK_CDX):
            return [], ["wayback: disallowed by robots.txt"]
        response = self._get(
            WAYBACK_CDX,
            {
                "url": ASHBY_HOST,
                "matchType": "domain",
                "fl": "original",
                "collapse": "urlkey",
                "filter": "statuscode:200",
                "limit": self.limit,
                "output": "text",
            },
        )
        response.raise_for_status()
        urls = (
            line.strip()
            for line in response.iter_lines(decode_unicode=True)
            if line and line.strip()
        )
        return _collect(urls), []


def build_archive_discoverers(
    cache_directory,
    *,
    user_agent: str,
    use_common_crawl: bool = True,
    use_wayback: bool = True,
    common_crawl_indexes: int = 2,
    wayback_limit: int = 200_000,
    timeout: float = 90.0,
    refresh_hours: int = 168,
    session: Optional[requests.Session] = None,
    robots: Optional[RobotsPolicy] = None,
) -> Sequence[_ArchiveDiscoverer]:
    providers: List[_ArchiveDiscoverer] = []
    if use_common_crawl:
        providers.append(
            CommonCrawlDiscoverer(
                cache_directory,
                user_agent=user_agent,
                indexes=common_crawl_indexes,
                timeout=timeout,
                refresh_hours=refresh_hours,
                session=session,
                robots=robots,
            )
        )
    if use_wayback:
        providers.append(
            WaybackDiscoverer(
                cache_directory,
                user_agent=user_agent,
                limit=wayback_limit,
                timeout=max(timeout, 180.0),
                refresh_hours=refresh_hours,
                session=session,
                robots=robots,
            )
        )
    return providers


__all__ = [
    "CommonCrawlDiscoverer",
    "WaybackDiscoverer",
    "build_archive_discoverers",
]
