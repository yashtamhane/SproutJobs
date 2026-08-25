"""On-disk cache of *extracted slugs* rather than fetched documents.

Several discovery sources return megabytes of JSON per query.  Keeping the
derived result instead of the raw body keeps the cache small — a few tens of
kilobytes for the whole system — while still sparing other people's servers a
refetch on every run.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import List, Optional, Sequence


CACHE_VERSION = 3


class SlugCache:
    """Read/write cached slug lists keyed by an arbitrary string."""

    def __init__(self, directory: Path | str, *, refresh_hours: int = 24) -> None:
        self.directory = Path(directory)
        self.refresh_seconds = max(0, int(refresh_hours)) * 3600

    def path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        return self.directory / (digest + ".json")

    def load(self, key: str, allow_stale: bool = False) -> Optional[List[str]]:
        try:
            payload = json.loads(self.path_for(key).read_text(encoding="utf-8"))
            if int(payload.get("cache_version", 0)) != CACHE_VERSION:
                return None
            age = time.time() - float(payload["fetched_at"])
            if allow_stale or age <= self.refresh_seconds:
                slugs = payload["slugs"]
                if isinstance(slugs, list):
                    return [str(slug) for slug in slugs]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return None

    def save(self, key: str, slugs: Sequence[str]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_version": CACHE_VERSION,
            "key": key,
            "fetched_at": time.time(),
            "slugs": list(slugs),
        }
        path = self.path_for(key)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


__all__ = ["CACHE_VERSION", "SlugCache"]
