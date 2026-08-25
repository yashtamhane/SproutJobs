from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobfinder.normalize import (
    ashby_board_url,
    extract_ashby_slug,
    html_to_text,
    normalize_ashby_job,
    normalize_timestamp,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_representative_ashby_payload() -> None:
    payload = json.loads((FIXTURES / "ashby_board.json").read_text(encoding="utf-8"))
    job = normalize_ashby_job(payload["jobs"][0], "example", payload["organizationName"])
    assert job.source_job_id == "job_001"
    assert job.company_name == "Example Labs"
    assert job.secondary_locations == ("Remote, US", "Jersey City, NJ")
    assert job.published_at == "2026-08-15T14:30:00Z"
    assert job.compensation_text == "$105,000 – $125,000 USD"
    assert job.job_url.endswith("/job_001")
    assert job.apply_url.endswith("/application")
    assert len(job.content_hash) == 64
    assert len(job.semantic_fingerprint) == 64


def test_html_to_text_preserves_bullets_and_ignores_script() -> None:
    result = html_to_text("<p>First</p><ul><li>One</li><li>Two &amp; three</li></ul><style>x{}</style>")
    assert "First" in result
    assert "- One" in result
    assert "- Two & three" in result
    assert "x{}" not in result


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("notion", "notion"),
        ("https://jobs.ashbyhq.com/notion/abc", "notion"),
        ("jobs.ashbyhq.com/notion", "notion"),
        ("https://sub.jobs.ashbyhq.com/notion", None),
        ("https://example.com/notion", None),
    ],
)
def test_source_slug_validation(value: str, expected: str) -> None:
    assert extract_ashby_slug(value) == expected


def test_board_url_rejects_path_or_foreign_host() -> None:
    assert ashby_board_url("acme-inc") == "https://jobs.ashbyhq.com/acme-inc"
    with pytest.raises(ValueError):
        ashby_board_url("https://example.com/acme")


def test_timestamp_handles_milliseconds() -> None:
    assert normalize_timestamp(1_787_000_000_000).endswith("Z")


def test_normalizer_requires_stable_identity() -> None:
    with pytest.raises(ValueError, match="stable id"):
        normalize_ashby_job({"title": "Software Engineer"}, "example")
