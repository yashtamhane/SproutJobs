"""Assemble the discovery plugins a configuration asks for.

Discovery is deliberately pluggable: each source is an independent object with a
``name`` and a ``discover()`` method, and none of them can break another.  A
source that fails, times out, or is disallowed contributes nothing and is
reported; the rest of the run continues.

Adding a source means writing one class and listing it here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

from .archives import CommonCrawlDiscoverer, WaybackDiscoverer
from .base import Discoverer
from .datasets import PublicDatasetDiscoverer, PublicPageDiscoverer
from .robots import RobotsPolicy
from .search import SearchEngineDiscoverer
from .seeds import SeedFileDiscoverer

LOGGER = logging.getLogger(__name__)

#: Human-readable descriptions used by the discovery report.
SOURCE_DESCRIPTIONS: dict[str, str] = {
    "public_dataset": "Public open-source job-list repositories",
    "public_page": "User-configured public web pages",
    "common_crawl": "Common Crawl public URL index",
    "wayback": "Internet Archive Wayback CDX index",
    "search_engine": "Permitted search endpoint (opt-in)",
    "user_seed_file": "Local seed file",
    "user_seed": "Added by hand with add-company / add-url",
}


def build_discoverers(
    config: Any,
    session: Optional[Any] = None,
    *,
    cache_directory: Optional[Path] = None,
) -> List[Discoverer]:
    """Build every enabled discovery plugin, in the order they should run."""

    discovery = config.discovery
    cache = cache_directory or config.discovery_cache_directory
    user_agent = config.network.user_agent
    robots = RobotsPolicy(
        user_agent=user_agent,
        timeout=min(config.network.request_timeout, 15.0),
        session=session,
        enabled=getattr(discovery, "respect_robots", True),
    )
    providers: List[Discoverer] = []

    if discovery.public_dataset_urls:
        providers.append(
            PublicDatasetDiscoverer(
                discovery.public_dataset_urls,
                cache,
                user_agent=user_agent,
                timeout=config.network.request_timeout,
                refresh_hours=discovery.refresh_hours,
                session=session,
                robots=robots,
            )
        )
    if discovery.public_page_urls:
        providers.append(
            PublicPageDiscoverer(
                discovery.public_page_urls,
                cache,
                user_agent=user_agent,
                timeout=config.network.request_timeout,
                refresh_hours=discovery.refresh_hours,
                session=session,
                robots=robots,
            )
        )
    if getattr(discovery, "use_common_crawl", False):
        providers.append(
            CommonCrawlDiscoverer(
                cache,
                user_agent=user_agent,
                indexes=getattr(discovery, "common_crawl_indexes", 2),
                timeout=max(config.network.request_timeout, 90.0),
                refresh_hours=getattr(discovery, "archive_refresh_hours", 168),
                session=session,
                robots=robots,
            )
        )
    if getattr(discovery, "use_wayback", False):
        providers.append(
            WaybackDiscoverer(
                cache,
                user_agent=user_agent,
                limit=getattr(discovery, "wayback_limit", 200_000),
                timeout=max(config.network.request_timeout, 180.0),
                refresh_hours=getattr(discovery, "archive_refresh_hours", 168),
                session=session,
                robots=robots,
            )
        )
    endpoints = list(getattr(discovery, "search_endpoints", ()) or ())
    if endpoints:
        providers.append(
            SearchEngineDiscoverer(
                endpoints,
                cache,
                user_agent=user_agent,
                timeout=config.network.request_timeout,
                refresh_hours=discovery.refresh_hours,
                session=session,
                robots=robots,
            )
        )
    seed_file = getattr(discovery, "seed_file", "") or ""
    if seed_file:
        path = Path(seed_file)
        if not path.is_absolute():
            path = config.project_root / path
        providers.append(SeedFileDiscoverer(path))

    LOGGER.debug(
        "Discovery plugins enabled: %s",
        ", ".join(provider.name for provider in providers) or "none",
    )
    return providers


def describe(source: str) -> str:
    return SOURCE_DESCRIPTIONS.get(source, source.replace("_", " ").capitalize())


__all__ = ["SOURCE_DESCRIPTIONS", "build_discoverers", "describe"]
