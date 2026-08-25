"""Respectful client for Ashby's unauthenticated public posting endpoint."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import requests

from ..config import DEFAULT_USER_AGENT
from ..models import BoardFetchResult, BoardValidation, NormalizedJob
from ..normalize import (
    ashby_board_url,
    extract_ashby_slug,
    humanize_slug,
    normalize_ashby_job,
    normalize_whitespace,
    utc_now,
)
from .base import (
    BoardNotFoundError,
    InvalidBoardResponseError,
    JobSource,
    SourceError,
    SourceHTTPError,
)


LOGGER = logging.getLogger(__name__)
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


class AshbySource(JobSource):
    """Fetch and normalize complete Ashby-hosted public job boards.

    ``retries`` counts retries after the initial request.  Retry behavior is kept in
    this class (rather than hidden in an HTTP adapter) so 429 ``Retry-After`` values
    are honored and injected sessions remain easy to test.
    """

    source_name = "ashby"
    api_template = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        backoff_factor: float = 0.75,
        min_request_interval: float = 0.15,
        include_compensation: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        if backoff_factor < 0:
            raise ValueError("backoff_factor cannot be negative")
        if min_request_interval < 0:
            raise ValueError("min_request_interval cannot be negative")
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.backoff_factor = float(backoff_factor)
        self.min_request_interval = float(min_request_interval)
        self.include_compensation = bool(include_compensation)
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self._owns_session = session is None
        self._sleep = sleep
        self._request_lock = threading.Lock()
        self._last_request_started = 0.0

    def __enter__(self) -> AshbySource:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def _normalize_slug(self, value: str) -> str:
        slug = extract_ashby_slug(value)
        if slug is None:
            raise ValueError(f"Invalid Ashby board slug or URL: {value!r}")
        return slug

    def _retry_delay(self, response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(max(float(retry_after), 0.0), 120.0)
                except ValueError:
                    try:
                        target = parsedate_to_datetime(retry_after)
                        delay = target.timestamp() - time.time()
                        return min(max(delay, 0.0), 120.0)
                    except (TypeError, ValueError, OverflowError):
                        pass
        # Small jitter prevents several workers from retrying in lockstep.
        base = self.backoff_factor * (2**attempt)
        return base + random.uniform(0, min(base * 0.2, 1.0)) if base else 0.0

    def _respect_rate_limit(self) -> None:
        """Space request starts even when one client is shared by worker threads."""

        if not self.min_request_interval:
            return
        with self._request_lock:
            now = time.monotonic()
            remaining = self.min_request_interval - (now - self._last_request_started)
            if remaining > 0:
                self._sleep(remaining)
            self._last_request_started = time.monotonic()

    def _request_payload(self, slug: str) -> Mapping[str, Any]:
        encoded_slug = quote(slug, safe="._~-")
        url = self.api_template.format(slug=encoded_slug)
        response: requests.Response | None = None
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                self._respect_rate_limit()
                response = self.session.get(
                    url,
                    params={
                        "includeCompensation": "true" if self.include_compensation else "false"
                    },
                    timeout=self.timeout,
                    headers={"Accept": "application/json", "User-Agent": self.user_agent},
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise SourceHTTPError(f"Ashby request failed for {slug}: {exc}") from exc
                self._sleep(self._retry_delay(None, attempt))
                continue

            if response.status_code == 404:
                raise BoardNotFoundError(
                    f"Ashby board not found: {slug}", status_code=response.status_code
                )
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < self.retries:
                LOGGER.debug(
                    "Retrying Ashby board %s after HTTP %s", slug, response.status_code
                )
                self._sleep(self._retry_delay(response, attempt))
                continue
            if not 200 <= response.status_code < 300:
                raise SourceHTTPError(
                    f"Ashby returned HTTP {response.status_code} for {slug}",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except (requests.JSONDecodeError, ValueError) as exc:
                raise InvalidBoardResponseError(
                    f"Ashby returned invalid JSON for {slug}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise InvalidBoardResponseError(
                    f"Ashby response for {slug} was not an object"
                )
            self._jobs_payload(payload, slug)  # verify completeness before returning
            return payload

        # The loop always returns or raises, but this makes the invariant explicit.
        raise SourceHTTPError(f"Ashby request failed for {slug}: {last_error}")

    @staticmethod
    def _jobs_payload(payload: Mapping[str, Any], slug: str) -> list[Mapping[str, Any]]:
        jobs_value: Any = None
        for key in ("jobs", "jobPostings", "postings"):
            if key in payload:
                jobs_value = payload[key]
                break
        if not isinstance(jobs_value, list):
            raise InvalidBoardResponseError(
                f"Ashby response for {slug} did not contain a complete jobs list"
            )
        if any(not isinstance(item, Mapping) for item in jobs_value):
            raise InvalidBoardResponseError(
                f"Ashby response for {slug} contained a malformed job"
            )
        return jobs_value

    @staticmethod
    def _display_name(payload: Mapping[str, Any], slug: str) -> str:
        """Use a name the payload states; otherwise derive a label from the slug.

        The documented posting endpoint returns only ``apiVersion`` and ``jobs``,
        so in practice the derived label is what boards get.  Nothing about the
        company is invented: the slug remains the stored source identity.
        """

        for key in ("organizationName", "companyName", "name"):
            name = normalize_whitespace(payload.get(key))
            if name:
                return name
        organization = payload.get("organization")
        if isinstance(organization, Mapping):
            name = normalize_whitespace(organization.get("name"))
            if name:
                return name
        return humanize_slug(slug)

    @staticmethod
    def _is_listed(raw_job: Mapping[str, Any]) -> bool:
        """Respect an explicit ``isListed = false``; assume listed when absent."""

        listed = raw_job.get("isListed")
        return True if listed is None else bool(listed)

    def validate_company(self, slug: str) -> BoardValidation:
        normalized = self._normalize_slug(slug)
        board_url = ashby_board_url(normalized)
        try:
            payload = self._request_payload(normalized)
            jobs = self._jobs_payload(payload, normalized)
        except BoardNotFoundError as exc:
            return BoardValidation(
                slug=normalized,
                valid=False,
                board_url=board_url,
                error=str(exc),
                status_code=exc.status_code,
            )
        except SourceHTTPError as exc:
            return BoardValidation(
                slug=normalized,
                valid=False,
                board_url=board_url,
                error=str(exc),
                status_code=exc.status_code,
            )
        except SourceError as exc:
            return BoardValidation(
                slug=normalized,
                valid=False,
                board_url=board_url,
                error=str(exc),
            )
        return BoardValidation(
            slug=normalized,
            valid=True,
            display_name=self._display_name(payload, normalized),
            job_count=len(jobs),
            board_url=board_url,
            status_code=200,
        )

    def normalize_job(
        self,
        raw_job: Mapping[str, Any],
        company_slug: str,
        company_name: str | None = None,
    ) -> NormalizedJob:
        return normalize_ashby_job(raw_job, company_slug, company_name)

    def fetch_company_jobs(self, slug: str) -> BoardFetchResult:
        normalized = self._normalize_slug(slug)
        payload = self._request_payload(normalized)
        raw_jobs = self._jobs_payload(payload, normalized)
        display_name = self._display_name(payload, normalized)
        jobs: list[NormalizedJob] = []
        ids: set[str] = set()
        try:
            for raw_job in raw_jobs:
                if not self._is_listed(raw_job):
                    continue
                job = self.normalize_job(raw_job, normalized, display_name)
                if job.source_job_id in ids:
                    raise ValueError(f"duplicate job id {job.source_job_id!r}")
                ids.add(job.source_job_id)
                jobs.append(job)
        except (TypeError, ValueError) as exc:
            # Never ingest a partial board: doing so could falsely close valid jobs.
            raise InvalidBoardResponseError(
                f"Ashby response for {normalized} could not be normalized completely: {exc}"
            ) from exc

        metadata = {
            key: value
            for key, value in payload.items()
            if key not in {"jobs", "jobPostings", "postings"}
        }
        return BoardFetchResult(
            source=self.source_name,
            slug=normalized,
            display_name=display_name,
            board_url=ashby_board_url(normalized),
            fetched_at=utc_now(),
            jobs=tuple(jobs),
            raw_metadata=metadata,
        )


# ``AshbyClient`` is a discoverable name for callers that think in HTTP clients;
# both names intentionally refer to the same implementation.
AshbyClient = AshbySource
