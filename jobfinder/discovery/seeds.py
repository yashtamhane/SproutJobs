"""Discovery from slugs the user supplied by hand.

A plain text file of slugs or board URLs, one per line, ``#`` for comments.
This is the escape hatch for a company the automated sources have never seen —
and it goes through exactly the same verification as everything else, so a
typo simply fails to verify rather than creating a phantom company.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .base import DiscoveryCandidate, DiscoveryResult
from .urls import extract_ashby_slug, is_probable_board_slug

LOGGER = logging.getLogger(__name__)


class SeedFileDiscoverer:
    """Read candidate slugs from a local file."""

    name = "user_seed_file"

    def __init__(self, path: Optional[Path | str]) -> None:
        self.path = Path(path) if path else None

    def discover(self, force: bool = False) -> DiscoveryResult:
        result = DiscoveryResult(source=self.name)
        if self.path is None or not self.path.exists():
            return result
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            result.error = f"{self.path}: {exc}"
            result.documents_failed = 1
            return result
        result.fetched = True
        result.documents_read = 1
        evidence = str(self.path)
        seen: set[str] = set()
        for raw in lines:
            value = raw.split("#", 1)[0].strip()
            if not value:
                continue
            slug = extract_ashby_slug(value) if "/" in value else value
            if not slug or not is_probable_board_slug(slug):
                LOGGER.debug("Ignoring unusable seed %r", raw.strip())
                continue
            key = slug.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.candidates.append(
                DiscoveryCandidate(slug=slug, source=self.name, evidence=evidence)
            )
        return result


__all__ = ["SeedFileDiscoverer"]
