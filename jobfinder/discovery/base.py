"""Interfaces shared by discovery strategies.

Every strategy answers the same narrow question — *which Ashby board slugs might
exist?* — and every answer is treated as an unverified guess.  A candidate only
becomes a company after Ashby's own public endpoint returns a real board for it,
which is what keeps a noisy source from polluting the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol


@dataclass(frozen=True)
class DiscoveryCandidate:
    slug: str
    source: str
    evidence: str


@dataclass
class DiscoveryResult:
    source: str
    candidates: List[DiscoveryCandidate] = field(default_factory=list)
    fetched: bool = False
    from_cache: bool = False
    error: Optional[str] = None
    # Documents actually read, for the per-source discovery report.
    documents_read: int = 0
    documents_failed: int = 0

    @property
    def slugs(self) -> set[str]:
        return {candidate.slug.casefold() for candidate in self.candidates}


class Discoverer(Protocol):
    name: str

    def discover(self, force: bool = False) -> DiscoveryResult:
        ...


__all__ = ["Discoverer", "DiscoveryCandidate", "DiscoveryResult"]
