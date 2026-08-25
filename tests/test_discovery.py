from __future__ import annotations

from pathlib import Path

import pytest

from jobfinder.discovery.datasets import PublicDatasetDiscoverer
from jobfinder.discovery.urls import extract_ashby_slug, extract_ashby_slugs, validate_slug


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://jobs.ashbyhq.com/notion", "notion"),
        ("https://jobs.ashbyhq.com/Linear/abc?x=1", "Linear"),
        ("jobs.ashbyhq.com/acme-inc", "acme-inc"),
        ("https://evil.example/jobs.ashbyhq.com/acme", None),
        ("https://jobs.ashbyhq.com/", None),
        ("https://jobs.ashbyhq.com/robots.txt", None),
    ],
)
def test_extract_ashby_slug(value: str, expected: str) -> None:
    assert extract_ashby_slug(value) == expected


def test_extract_slugs_handles_encoded_and_deduplicates() -> None:
    text = """
    [one](https://jobs.ashbyhq.com/one/abc)
    redirect=https%3A%2F%2Fjobs.ashbyhq.com%2Ftwo%2Fdef
    https://jobs.ashbyhq.com/one/another
    """
    assert extract_ashby_slugs(text) == ["one", "two"]


@pytest.mark.parametrize("slug", ["acme", "acme-inc", "A.B_c", "123"])
def test_valid_slug(slug: str) -> None:
    assert validate_slug(slug)


@pytest.mark.parametrize("slug", ["", "../escape", "spaces here", "robots.txt", "x" * 101])
def test_invalid_slug(slug: str) -> None:
    assert not validate_slug(slug)


class FakeResponse:
    text = "https://jobs.ashbyhq.com/acme https://jobs.ashbyhq.com/second"

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return FakeResponse()


def test_dataset_discovery_uses_cache(tmp_path: Path) -> None:
    session = FakeSession()
    discoverer = PublicDatasetDiscoverer(
        ["https://example.test/list.md"],
        tmp_path,
        user_agent="test",
        session=session,
    )
    first = discoverer.discover()
    second = discoverer.discover()
    assert [candidate.slug for candidate in first.candidates] == ["acme", "second"]
    assert [candidate.slug for candidate in second.candidates] == ["acme", "second"]
    assert second.from_cache
    assert session.calls == 1
