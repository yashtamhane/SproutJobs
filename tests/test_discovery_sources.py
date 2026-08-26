"""The pluggable multi-source discovery system.

Everything here runs offline against fake sessions.  What is being checked is
the contract each source must honor: it produces *candidates only*, it filters
obvious archive noise, it never guesses at slugs, it honors robots.txt, and a
single failing source never takes down the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobfinder.config import AppConfig
from jobfinder.db import Database
from jobfinder.discovery.archives import CommonCrawlDiscoverer, WaybackDiscoverer
from jobfinder.discovery.datasets import PublicPageDiscoverer
from jobfinder.discovery.registry import build_discoverers
from jobfinder.discovery.robots import RobotsPolicy
from jobfinder.discovery.seeds import SeedFileDiscoverer
from jobfinder.discovery.urls import extract_ashby_slugs, is_probable_board_slug
from jobfinder.models import CompanyCandidate
from jobfinder.pipeline import discovery_report, run_discovery


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200, payload: Any = None) -> None:
        self.text = text
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload if self._payload is not None else json.loads(self.text)

    def iter_lines(self, decode_unicode: bool = False):
        yield from self.text.splitlines()


class FakeSession:
    """Routes by substring so each test can describe only what it cares about."""

    def __init__(self, routes: dict[str, FakeResponse]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def get(self, url: str, params: Any = None, **kwargs: Any) -> FakeResponse:
        self.requested.append(url)
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        return FakeResponse(status_code=404)


class StubDiscoverer:
    def __init__(self, name: str, slugs: list[str], error: str | None = None) -> None:
        self.name = name
        self.slugs = slugs
        self.error = error

    def discover(self, force: bool = False):
        from jobfinder.discovery.base import DiscoveryCandidate, DiscoveryResult

        result = DiscoveryResult(source=self.name, fetched=True, documents_read=1)
        result.error = self.error
        result.candidates = [
            DiscoveryCandidate(slug=slug, source=self.name, evidence=f"{self.name}-doc")
            for slug in self.slugs
        ]
        return result


class ExplodingDiscoverer:
    name = "exploding"

    def discover(self, force: bool = False):
        raise RuntimeError("source is down")


@pytest.fixture()
def store(tmp_path: Path) -> Database:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    yield database
    database.close()


class TestSlugFiltering:
    def test_archive_telemetry_paths_are_rejected(self) -> None:
        """Web archives are full of Ashby's own client-state URLs."""

        assert not is_probable_board_slug("root.f47a8873_e273_4883_a52a_a2f901455b39")
        assert not is_probable_board_slug("root.81ce5dc3_ed8a_4bec_a151_61b49e76986f")
        assert not is_probable_board_slug("6eec82ac-9713-512d-ac2e-405618935375")
        assert not is_probable_board_slug("assets")

    def test_real_slugs_survive(self) -> None:
        for slug in ("notion", "1password", "helm-ai", "claim-health", "0g"):
            assert is_probable_board_slug(slug), slug

    def test_strict_extraction_drops_the_noise(self) -> None:
        text = (
            "https://jobs.ashbyhq.com/notion/abc "
            "https://jobs.ashbyhq.com/root.f47a8873_e273_4883_a52a_a2f901455b39 "
            "https://jobs.ashbyhq.com/linear"
        )
        assert extract_ashby_slugs(text, strict=True) == ["notion", "linear"]


class TestArchiveDiscoverers:
    def test_common_crawl_reads_the_public_index(self, tmp_path: Path) -> None:
        session = FakeSession(
            {
                "collinfo.json": FakeResponse(payload=[{"id": "CC-MAIN-2026-30"}]),
                "CC-MAIN-2026-30-index": FakeResponse(
                    text="\n".join(
                        json.dumps({"url": url})
                        for url in (
                            "https://jobs.ashbyhq.com/notion",
                            "https://jobs.ashbyhq.com/notion/abc-123",
                            "https://jobs.ashbyhq.com/linear/def?utm_source=x",
                            "https://jobs.ashbyhq.com/root.f47a8873_e273_4883_a52a_a2f901455b39",
                            "https://example.com/not-ashby",
                        )
                    )
                ),
            }
        )
        discoverer = CommonCrawlDiscoverer(
            tmp_path, user_agent="test", indexes=1, session=session
        )
        result = discoverer.discover(force=True)
        assert [item.slug for item in result.candidates] == ["notion", "linear"]
        assert result.fetched

    def test_wayback_reads_the_public_cdx_index(self, tmp_path: Path) -> None:
        session = FakeSession(
            {
                "web.archive.org": FakeResponse(
                    text="\n".join(
                        (
                            "https://jobs.ashbyhq.com/ramp",
                            "https://jobs.ashbyhq.com/ramp/job-1",
                            "https://jobs.ashbyhq.com/$10.2K",
                            "https://jobs.ashbyhq.com/bifrost",
                        )
                    )
                )
            }
        )
        result = WaybackDiscoverer(tmp_path, user_agent="test", session=session).discover(
            force=True
        )
        assert [item.slug for item in result.candidates] == ["ramp", "bifrost"]

    def test_a_second_run_is_served_from_cache(self, tmp_path: Path) -> None:
        session = FakeSession(
            {"web.archive.org": FakeResponse(text="https://jobs.ashbyhq.com/ramp")}
        )
        discoverer = WaybackDiscoverer(tmp_path, user_agent="test", session=session)
        discoverer.discover(force=True)
        calls = len(session.requested)
        again = discoverer.discover()
        assert again.from_cache
        assert len(session.requested) == calls, "cache must spare the archive a refetch"

    def test_a_network_failure_falls_back_to_the_cached_slugs(self, tmp_path: Path) -> None:
        import requests

        class FlakySession(FakeSession):
            def __init__(self) -> None:
                super().__init__({})
                self.fail = False

            def get(self, url: str, params: Any = None, **kwargs: Any) -> FakeResponse:
                if self.fail:
                    raise requests.RequestException("network down")
                return FakeResponse(text="https://jobs.ashbyhq.com/ramp")

        session = FlakySession()
        discoverer = WaybackDiscoverer(tmp_path, user_agent="test", session=session)
        discoverer.discover(force=True)
        session.fail = True
        result = discoverer.discover(force=True)
        assert [item.slug for item in result.candidates] == ["ramp"]
        assert result.from_cache


class TestRobots:
    def test_a_disallowed_page_is_skipped_not_worked_around(self, tmp_path: Path) -> None:
        session = FakeSession(
            {
                "robots.txt": FakeResponse(text="User-agent: *\nDisallow: /\n"),
                "example.com/jobs": FakeResponse(text="https://jobs.ashbyhq.com/notion"),
            }
        )
        robots = RobotsPolicy(user_agent="test", session=session)
        discoverer = PublicPageDiscoverer(
            ["https://example.com/jobs"],
            tmp_path,
            user_agent="test",
            session=session,
            robots=robots,
        )
        result = discoverer.discover(force=True)
        assert result.candidates == []
        assert "robots.txt" in (result.error or "")
        assert not any("example.com/jobs" in url for url in session.requested)

    def test_an_allowed_page_is_read(self, tmp_path: Path) -> None:
        session = FakeSession(
            {
                "robots.txt": FakeResponse(text="User-agent: *\nAllow: /\n"),
                "example.com/jobs": FakeResponse(text="https://jobs.ashbyhq.com/notion"),
            }
        )
        robots = RobotsPolicy(user_agent="test", session=session)
        discoverer = PublicPageDiscoverer(
            ["https://example.com/jobs"],
            tmp_path,
            user_agent="test",
            session=session,
            robots=robots,
        )
        assert [item.slug for item in discoverer.discover(force=True).candidates] == ["notion"]

    def test_a_missing_robots_file_is_not_a_prohibition(self, tmp_path: Path) -> None:
        session = FakeSession(
            {"example.com/jobs": FakeResponse(text="https://jobs.ashbyhq.com/notion")}
        )
        robots = RobotsPolicy(user_agent="test", session=session)
        assert robots.allows("https://example.com/jobs") is True


class TestSeedFile:
    def test_slugs_and_urls_are_both_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "seeds.txt"
        path.write_text(
            "# a comment\nnotion\nhttps://jobs.ashbyhq.com/linear\n\n  ramp  # trailing\n",
            encoding="utf-8",
        )
        result = SeedFileDiscoverer(path).discover()
        assert [item.slug for item in result.candidates] == ["notion", "linear", "ramp"]

    def test_a_missing_file_is_silent(self, tmp_path: Path) -> None:
        result = SeedFileDiscoverer(tmp_path / "nope.txt").discover()
        assert result.candidates == []
        assert result.error is None


class TestRegistry:
    def test_every_configured_source_is_built(self, tmp_path: Path) -> None:
        config = AppConfig()
        config.project_root = tmp_path
        config.database.path = str(tmp_path / "jobs.sqlite3")
        config.discovery.public_page_urls = ["https://example.com/list"]
        config.discovery.search_endpoints = ["https://search.example/?q={query}"]
        config.discovery.use_common_crawl = True
        config.discovery.use_wayback = True
        names = {provider.name for provider in build_discoverers(config)}
        assert {
            "public_dataset",
            "public_page",
            "common_crawl",
            "wayback",
            "search_engine",
            "user_seed_file",
        } <= names

    def test_common_crawl_is_off_by_default(self) -> None:
        """Their robots.txt disallows the index endpoint, so we do not ask."""

        assert AppConfig().discovery.use_common_crawl is False

    def test_disabled_sources_are_left_out(self, tmp_path: Path) -> None:
        config = AppConfig()
        config.project_root = tmp_path
        config.database.path = str(tmp_path / "jobs.sqlite3")
        config.discovery.use_common_crawl = False
        config.discovery.use_wayback = False
        config.discovery.seed_file = ""
        names = {provider.name for provider in build_discoverers(config)}
        assert "common_crawl" not in names and "wayback" not in names
        assert "user_seed_file" not in names

    def test_search_is_off_unless_an_endpoint_is_configured(self, tmp_path: Path) -> None:
        """Automated querying is opt-in; nothing ships pointed at a search engine."""

        config = AppConfig()
        config.project_root = tmp_path
        config.database.path = str(tmp_path / "jobs.sqlite3")
        assert config.discovery.search_endpoints == []
        assert "search_engine" not in {p.name for p in build_discoverers(config)}


class TestProvenanceAndReporting:
    def test_each_source_is_recorded_per_slug(self, store: Database) -> None:
        config = AppConfig()
        run_id = store.start_run("test")
        run_discovery(
            config,
            store,
            run_id,
            discoverers=[
                StubDiscoverer("alpha", ["notion", "linear", "ramp"]),
                StubDiscoverer("beta", ["ramp", "vercel"]),
            ],
        )
        stats = {row["discovery_source"]: row for row in store.discovery_source_stats()}
        assert stats["alpha"]["unique_slugs"] == 3
        assert stats["beta"]["unique_slugs"] == 2

    def test_overlap_between_sources_is_measured(self, store: Database) -> None:
        config = AppConfig()
        run_id = store.start_run("test")
        run_discovery(
            config,
            store,
            run_id,
            discoverers=[
                StubDiscoverer("alpha", ["notion", "linear", "ramp"]),
                StubDiscoverer("beta", ["ramp", "vercel"]),
            ],
        )
        overlap = store.discovery_source_overlap()
        assert overlap["total_unique_slugs"] == 4
        pair = overlap["pairs"][0]
        assert {pair["left_source"], pair["right_source"]} == {"alpha", "beta"}
        assert pair["shared"] == 1
        assert overlap["exclusive"]["alpha"] == 2
        assert overlap["exclusive"]["beta"] == 1

    def test_a_known_slug_still_records_its_source(self, store: Database) -> None:
        """Otherwise the second source to find a board looks like it found nothing."""

        store.register_company_candidate(
            CompanyCandidate(slug="notion", discovery_source="alpha")
        )
        config = AppConfig()
        run_id = store.start_run("test")
        outcome = run_discovery(
            config, store, run_id, discoverers=[StubDiscoverer("beta", ["notion"])]
        )
        assert outcome.new_slugs == 0
        stats = {row["discovery_source"]: row for row in store.discovery_source_stats()}
        assert stats["beta"]["unique_slugs"] == 1

    def test_one_failing_source_never_stops_the_others(self, store: Database) -> None:
        config = AppConfig()
        run_id = store.start_run("test")
        outcome = run_discovery(
            config,
            store,
            run_id,
            discoverers=[
                ExplodingDiscoverer(),
                StubDiscoverer("alpha", ["notion", "linear"]),
            ],
        )
        assert outcome.new_slugs == 2
        assert any("source is down" in error for error in outcome.errors)
        by_source = {item.source: item for item in outcome.per_source}
        assert by_source["exploding"].error
        assert by_source["alpha"].new_slugs == 2

    def test_the_report_states_verified_boards_separately(self, store: Database) -> None:
        config = AppConfig()
        run_id = store.start_run("test")
        run_discovery(
            config,
            store,
            run_id,
            discoverers=[StubDiscoverer("alpha", ["notion", "ghost"])],
        )
        store.record_company_success("notion", display_name="Notion", job_count=3)
        report = discovery_report(store)
        assert report["companies_registered"] == 2
        assert report["boards_verified"] == 1
        alpha = next(row for row in report["sources"] if row["discovery_source"] == "alpha")
        assert alpha["unique_slugs"] == 2
        assert alpha["verified"] == 1

    def test_candidates_are_never_treated_as_verified_companies(self, store: Database) -> None:
        config = AppConfig()
        run_id = store.start_run("test")
        run_discovery(config, store, run_id, discoverers=[StubDiscoverer("alpha", ["ghost"])])
        company = store.get_company("ghost")
        assert company is not None
        assert not company["active"], "an unverified candidate must not be active"
        assert company["last_success_at"] is None


class TestRobotsWildcards:
    """The stdlib parser ignores * and $, which is wrong in both directions."""

    def _policy(self, text: str):
        from jobfinder.discovery.robots import _Policy

        return _Policy(text)

    def test_a_dollar_anchored_allow_beats_a_blanket_disallow(self) -> None:
        policy = self._policy(
            "User-agent: *\nDisallow: /\n\nAllow: /$\nAllow: /collinfo.json$\n"
        )
        assert policy.allows("SproutJobs/1.0", "/collinfo.json") is True
        assert policy.allows("SproutJobs/1.0", "/") is True

    def test_the_blanket_disallow_still_covers_everything_else(self) -> None:
        policy = self._policy(
            "User-agent: *\nDisallow: /\n\nAllow: /$\nAllow: /collinfo.json$\n"
        )
        assert policy.allows("SproutJobs/1.0", "/CC-MAIN-2026-30-index?url=x") is False
        assert policy.allows("SproutJobs/1.0", "/collinfo.json.bak") is False

    def test_a_star_wildcard_matches_a_query_string(self) -> None:
        policy = self._policy("User-agent: *\nDisallow: /*?\nAllow: /?*\n")
        assert policy.allows("bot", "/search?q=x") is False
        assert policy.allows("bot", "/about") is True

    def test_an_empty_disallow_forbids_nothing(self) -> None:
        policy = self._policy("User-agent: *\nDisallow:\n")
        assert policy.allows("bot", "/anything") is True

    def test_common_crawl_is_skipped_and_says_so(self, tmp_path: Path) -> None:
        """A refusal is reported, never routed around."""

        session = FakeSession(
            {"robots.txt": FakeResponse(text="User-agent: *\nDisallow: /\n")}
        )
        from jobfinder.discovery.robots import RobotsPolicy

        discoverer = CommonCrawlDiscoverer(
            tmp_path,
            user_agent="test",
            session=session,
            robots=RobotsPolicy(user_agent="test", session=session),
        )
        result = discoverer.discover(force=True)
        assert result.candidates == []
        assert "robots.txt" in (result.error or "")
        assert not any("collinfo" in url for url in session.requested)
