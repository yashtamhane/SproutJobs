"""Interface and errors shared by public applicant-tracking-system adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from ..models import BoardFetchResult, BoardValidation, NormalizedJob


class SourceError(RuntimeError):
    """Base class for a recoverable source fetch/normalization failure."""


class SourceHTTPError(SourceError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BoardNotFoundError(SourceHTTPError):
    """The source confirms that a candidate board does not exist."""


class InvalidBoardResponseError(SourceError):
    """A response cannot safely be treated as a complete public job board."""


class JobSource(ABC):
    """Minimal adapter boundary for current and future ATS integrations."""

    source_name: str

    @abstractmethod
    def validate_company(self, slug: str) -> BoardValidation:
        """Verify a candidate without mutating the company registry."""

    @abstractmethod
    def fetch_company_jobs(self, slug: str) -> BoardFetchResult:
        """Fetch an entire current public board or raise :class:`SourceError`."""

    @abstractmethod
    def normalize_job(
        self,
        raw_job: Mapping[str, Any],
        company_slug: str,
        company_name: str | None = None,
    ) -> NormalizedJob:
        """Translate one raw source job to the normalized model."""

    def discover_companies(self) -> tuple[object, ...]:
        """Adapters may expose source-native discovery; Ashby uses plugins instead."""

        return ()
