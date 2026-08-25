"""Tests for the Ashby adapter's HTTP behavior and board validation.

Every test drives a fake session; nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from jobfinder.sources.ashby import AshbySource
from jobfinder.sources.base import (
    BoardNotFoundError,
    InvalidBoardResponseError,
    SourceHTTPError,
)


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, body: str | None = None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is not None:
            return json.loads(self._body)
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Return queued responses (or raise queued exceptions) in order."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.requests.append((url, dict(params or {})))
        result = self.queue.pop(0) if self.queue else FakeResponse(200, {"jobs": []})
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        return None


def board_payload() -> dict:
    return json.loads((FIXTURES / "ashby_board.json").read_text(encoding="utf-8"))


def make_source(session: FakeSession, **overrides) -> AshbySource:
    options = {
        "session": session,
        "retries": 2,
        "backoff_factor": 0.0,
        "min_request_interval": 0.0,
        "sleep": lambda _seconds: None,
    }
    options.update(overrides)
    return AshbySource(**options)


class TestFetching:
    def test_a_board_is_normalized_completely(self) -> None:
        session = FakeSession(FakeResponse(200, board_payload()))
        board = make_source(session).fetch_company_jobs("example")

        assert board.slug == "example"
        assert board.board_url == "https://jobs.ashbyhq.com/example"
        assert len(board.jobs) == 1
        assert board.jobs[0].source_job_id == "job_001"
        assert board.jobs[0].company_name == "Example Labs"
        assert board.fetched_at.endswith("Z")

    def test_the_documented_endpoint_and_compensation_flag_are_used(self) -> None:
        session = FakeSession(FakeResponse(200, board_payload()))
        make_source(session).fetch_company_jobs("example")

        url, params = session.requests[0]
        assert url == "https://api.ashbyhq.com/posting-api/job-board/example"
        assert params == {"includeCompensation": "true"}

    def test_a_board_url_is_accepted_in_place_of_a_slug(self) -> None:
        session = FakeSession(FakeResponse(200, board_payload()))
        board = make_source(session).fetch_company_jobs("https://jobs.ashbyhq.com/example/job_001")
        assert board.slug == "example"

    def test_a_foreign_host_is_refused_before_any_request(self) -> None:
        session = FakeSession()
        with pytest.raises(ValueError):
            make_source(session).fetch_company_jobs("https://example.com/acme")
        assert session.requests == []

    def test_unlisted_postings_are_excluded(self) -> None:
        payload = board_payload()
        hidden = dict(payload["jobs"][0])
        hidden["id"] = "job_002"
        hidden["isListed"] = False
        payload["jobs"].append(hidden)

        board = make_source(FakeSession(FakeResponse(200, payload))).fetch_company_jobs("example")
        assert [job.source_job_id for job in board.jobs] == ["job_001"]

    def test_the_display_name_falls_back_to_a_readable_slug(self) -> None:
        payload = {"apiVersion": 1, "jobs": []}
        board = make_source(FakeSession(FakeResponse(200, payload))).fetch_company_jobs("acme-labs")
        assert board.display_name == "Acme Labs"

    def test_an_empty_board_is_valid_and_not_an_error(self) -> None:
        board = make_source(
            FakeSession(FakeResponse(200, {"apiVersion": 1, "jobs": []}))
        ).fetch_company_jobs("example")
        assert board.jobs == ()


class TestFailureHandling:
    def test_a_404_is_a_definitive_missing_board(self) -> None:
        session = FakeSession(FakeResponse(404))
        with pytest.raises(BoardNotFoundError):
            make_source(session).fetch_company_jobs("nope")
        assert len(session.requests) == 1, "a missing board must not be retried"

    def test_server_errors_are_retried_then_surfaced(self) -> None:
        session = FakeSession(FakeResponse(500), FakeResponse(500), FakeResponse(500))
        with pytest.raises(SourceHTTPError):
            make_source(session, retries=2).fetch_company_jobs("example")
        assert len(session.requests) == 3

    def test_a_retry_succeeds_after_a_transient_failure(self) -> None:
        session = FakeSession(FakeResponse(503), FakeResponse(200, board_payload()))
        board = make_source(session).fetch_company_jobs("example")
        assert len(board.jobs) == 1
        assert len(session.requests) == 2

    def test_rate_limiting_is_honored_rather_than_evaded(self) -> None:
        delays: list[float] = []
        session = FakeSession(
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(200, board_payload()),
        )
        source = make_source(session, sleep=delays.append)
        source.fetch_company_jobs("example")
        assert 7.0 in delays, "the server's Retry-After delay should be respected"

    def test_network_errors_are_wrapped_not_leaked(self) -> None:
        session = FakeSession(
            requests.ConnectionError("dns"), requests.ConnectionError("dns"),
            requests.ConnectionError("dns"),
        )
        with pytest.raises(SourceHTTPError):
            make_source(session, retries=2).fetch_company_jobs("example")

    @pytest.mark.parametrize(
        "payload",
        [
            {"apiVersion": 1},
            {"jobs": {"not": "a list"}},
            {"jobs": ["not a mapping"]},
        ],
    )
    def test_an_incomplete_board_is_rejected_rather_than_ingested(self, payload) -> None:
        with pytest.raises(InvalidBoardResponseError):
            make_source(FakeSession(FakeResponse(200, payload))).fetch_company_jobs("example")

    def test_invalid_json_is_rejected(self) -> None:
        session = FakeSession(FakeResponse(200, body="{not json"))
        with pytest.raises(InvalidBoardResponseError):
            make_source(session).fetch_company_jobs("example")

    def test_a_job_without_a_stable_id_fails_the_whole_board(self) -> None:
        """A partial board could otherwise close postings that still exist."""

        payload = board_payload()
        payload["jobs"].append({"title": "Software Engineer"})
        with pytest.raises(InvalidBoardResponseError):
            make_source(FakeSession(FakeResponse(200, payload))).fetch_company_jobs("example")


class TestValidation:
    def test_a_real_board_validates_with_its_job_count(self) -> None:
        validation = make_source(
            FakeSession(FakeResponse(200, board_payload()))
        ).validate_company("example")

        assert validation.valid
        assert validation.job_count == 1
        assert validation.display_name == "Example Labs"
        assert validation.board_url == "https://jobs.ashbyhq.com/example"

    def test_validation_reports_failure_without_raising(self) -> None:
        validation = make_source(FakeSession(FakeResponse(404))).validate_company("nope")
        assert not validation.valid
        assert validation.status_code == 404
        assert validation.error

    def test_a_transient_failure_is_reported_as_unverified_not_missing(self) -> None:
        session = FakeSession(FakeResponse(500), FakeResponse(500), FakeResponse(500))
        validation = make_source(session, retries=2).validate_company("example")
        assert not validation.valid
        assert validation.status_code == 500
