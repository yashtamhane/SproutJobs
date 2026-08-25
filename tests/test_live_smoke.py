"""Live smoke test against real Ashby boards.

These are the only tests that touch the network and they are excluded from the
default run.  Execute them deliberately::

    python -m pytest -m integration

They read public data only, one board at a time, with the project's normal
rate limiting and User-Agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobfinder.config import load_config
from jobfinder.db import Database
from jobfinder.pipeline import build_source, run_pipeline
from jobfinder.scoring import analyze_normalized_job
from jobfinder.sources.base import SourceError


pytestmark = pytest.mark.integration

# Long-established public Ashby boards.
LIVE_SLUGS = ("notion", "linear", "ramp")


@pytest.fixture(scope="module")
def source():
    config = load_config(Path(__file__).resolve().parents[1] / "config.toml")
    adapter = build_source(config)
    yield adapter
    adapter.close()


@pytest.mark.parametrize("slug", LIVE_SLUGS)
def test_public_board_returns_usable_postings(source, slug: str) -> None:
    board = source.fetch_company_jobs(slug)

    assert board.slug == slug
    assert board.jobs, f"{slug} published no jobs"
    for job in board.jobs:
        assert job.source_job_id
        assert job.title
        assert job.source == "ashby"
        if job.apply_url:
            assert job.apply_url.startswith("https://jobs.ashbyhq.com/")
        if job.job_url:
            assert job.job_url.startswith("https://jobs.ashbyhq.com/")


def test_apply_links_point_at_the_posting_they_belong_to(source) -> None:
    board = source.fetch_company_jobs("linear")
    for job in board.jobs:
        if job.apply_url:
            assert f"/{board.slug}/" in job.apply_url
            assert job.source_job_id in job.apply_url


def test_a_nonexistent_board_is_reported_not_invented(source) -> None:
    validation = source.validate_company("this-board-should-not-exist-jf-test")
    assert not validation.valid
    assert validation.error


def test_analysis_runs_over_real_descriptions(source) -> None:
    board = source.fetch_company_jobs("notion")
    results = [analyze_normalized_job(job, attach=False) for job in board.jobs]

    assert any(result.is_software_role for result in results), "expected some software roles"
    assert all(0 <= result.score <= 100 for result in results)
    assert all(
        result.category in {"strong_match", "possible_match", "weak_match", "likely_ineligible"}
        for result in results
    )


def test_live_end_to_end_run_is_idempotent(tmp_path: Path) -> None:
    """Two consecutive live runs must not duplicate jobs or rewrite first_seen."""

    (tmp_path / "config.toml").write_text(
        "[discovery]\nenabled = false\npublic_dataset_urls = []\n", encoding="utf-8"
    )
    config = load_config(tmp_path / "config.toml")

    database = Database(config.database_path)
    database.initialize()
    for slug in LIVE_SLUGS:
        database.upsert_company(slug, verified=True)
    database.close()

    first = run_pipeline(config, skip_discovery=True)
    assert first.boards_succeeded == len(LIVE_SLUGS)
    assert first.jobs_inserted > 0

    database = Database(config.database_path)
    try:
        before = {row["id"]: row["first_seen_at"] for row in database.list_jobs(active=None)}
    finally:
        database.close()

    second = run_pipeline(config, skip_discovery=True)
    assert second.jobs_inserted == 0, "a second run must not re-insert the same postings"

    database = Database(config.database_path)
    try:
        after = {row["id"]: row["first_seen_at"] for row in database.list_jobs(active=None)}
    finally:
        database.close()

    assert after == before, "first_seen_at must never be rewritten"
    assert Path(second.dashboard_path).exists()
    assert Path(second.csv_path).exists()


def test_discovery_sources_yield_verifiable_slugs() -> None:
    """At least one automatic source must produce slugs that Ashby confirms."""

    from jobfinder.pipeline import build_discoverers

    config = load_config(Path(__file__).resolve().parents[1] / "config.toml")
    candidates: list[str] = []
    for discoverer in build_discoverers(config):
        candidates.extend(item.slug for item in discoverer.discover().candidates)

    assert candidates, "discovery produced no candidates"

    adapter = build_source(config)
    verified = 0
    try:
        for slug in candidates[:8]:
            try:
                if adapter.validate_company(slug).valid:
                    verified += 1
            except SourceError:
                continue
    finally:
        adapter.close()
    assert verified, "no discovered slug could be verified against Ashby"
