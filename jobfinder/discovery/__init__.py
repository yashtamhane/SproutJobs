"""Pluggable Ashby company discovery from free public sources."""

from .archives import CommonCrawlDiscoverer, WaybackDiscoverer
from .base import DiscoveryCandidate, DiscoveryResult, Discoverer
from .cache import SlugCache
from .datasets import PublicDatasetDiscoverer, PublicPageDiscoverer
from .registry import SOURCE_DESCRIPTIONS, build_discoverers, describe
from .robots import RobotsPolicy
from .search import SearchEngineDiscoverer
from .seeds import SeedFileDiscoverer
from .urls import (
    extract_ashby_slug,
    extract_ashby_slugs,
    is_probable_board_slug,
    validate_slug,
)

__all__ = [
    "SOURCE_DESCRIPTIONS",
    "CommonCrawlDiscoverer",
    "DiscoveryCandidate",
    "DiscoveryResult",
    "Discoverer",
    "PublicDatasetDiscoverer",
    "PublicPageDiscoverer",
    "RobotsPolicy",
    "SearchEngineDiscoverer",
    "SeedFileDiscoverer",
    "SlugCache",
    "WaybackDiscoverer",
    "build_discoverers",
    "describe",
    "extract_ashby_slug",
    "extract_ashby_slugs",
    "is_probable_board_slug",
    "validate_slug",
]
