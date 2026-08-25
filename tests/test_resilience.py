"""A public data source going away must not take the application with it.

SproutJobs reads other people's servers, and those servers will be down, slow,
reorganised or discontinued at some point. These tests pin the behavior that
matters when that happens: providers fail independently, the failure is
explained rather than swallowed, boards that already verified still refresh,
and a fresh database says something a person can act on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from jobfinder.cli import main as cli_main
from jobfinder.config import AppConfig
from jobfinder.db import Database
from jobfinder.discovery.base import DiscoveryCandidate, DiscoveryResult
from jobfinder.discovery.datasets import PublicDatasetDiscoverer
from jobfinder.models import BoardFetchResult, NormalizedJob
from jobfinder.pipeline import run_discovery, run_pipeline
from jobfinder.sources.base import SourceHTTPError


class WorkingDiscoverer:
    def __init__(self, name: str, slugs: list[str]) -> None:
        self.name = name
        self._slugs = slugs

    def discover(self, force: bool = False) -> DiscoveryResult:
        return DiscoveryResult(
            source=self.name,
            fetched=True,
            documents_read=1,
            candidates=[
                DiscoveryCandidate(slug=slug, source=self.name, evidence="fixture")
                for slug in self._slugs
            ],
        )


class ExplodingDiscoverer:
    """A provider whose host has gone away entirely."""

    def __init__(self, name: str = "offline_source") -> None:
        self.name = name

    def discover(self, force: bool = False) -> DiscoveryResult:
        raise requests.ConnectionError("dataset.example: Name or service not known")


class ReportingFailureDiscoverer:
    """A provider that survives but could not read some of its documents."""

    name = "partly_broken"

    def discover(self, force: bool = False) -> DiscoveryResult:
        return DiscoveryResult(
            source=self.name,
            fetched=True,
            documents_read=1,
            documents_failed=2,
            error="https://host.example/a.md: HTTP 503",
            candidates=[
                DiscoveryCandidate(slug="survivor", source=self.name, evidence="a")
            ],
        )


class FakeSource:
    """Minimal Ashby stand-in: some slugs resolve, others fail."""

    source_name = "ashby"

    def __init__(self, boards: dict[str, list[str]]) -> None:
        self.boards = boards
        self.requested: list[str] = []

    def fetch_company_jobs(self, slug: str) -> BoardFetchResult:
        self.requested.append(slug)
        if slug not in self.boards:
            raise SourceHTTPError(f"Ashby board not found: {slug}", status_code=404)
        jobs = [
            NormalizedJob(
                source="ashby",
                source_company_slug=slug,
                source_job_id=f"{slug}-{index}",
                company_name=slug.title(),
                title=title,
                location="New York, NY",
                description_text="0-2 years of experience. BS in Computer Science.",
                content_hash=f"{slug}-{index}-hash",
            )
            for index, title in enumerate(self.boards[slug])
        ]
        return BoardFetchResult(
            source="ashby",
            slug=slug,
            display_name=slug.title(),
            board_url=f"https://jobs.ashbyhq.com/{slug}",
            fetched_at="2026-08-24T00:00:00Z",
            jobs=tuple(jobs),
        )

    def close(self) -> None:
        pass


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.project_root = tmp_path
    config.database.path = "data/jobs.sqlite3"
    config.output.directory = "output"
    config.discovery.seed_file = ""
    config.network.min_request_interval = 0
    return config


@pytest.fixture()
def store(config: AppConfig) -> Database:
    database = Database(config.database_path)
    database.initialize()
    yield database
    database.close()


class TestProvidersFailIndependently:
    def test_a_dead_provider_does_not_stop_the_others(
        self, config: AppConfig, store: Database
    ) -> None:
        outcome = run_discovery(
            config,
            store,
            store.start_run("test"),
            discoverers=[
                ExplodingDiscoverer("dead_one"),
                WorkingDiscoverer("live_one", ["acme", "globex"]),
                ExplodingDiscoverer("dead_two"),
            ],
        )
        assert outcome.new_slugs == 2, "the healthy provider still registered its slugs"
        assert {row["slug"] for row in store.list_companies(source="ashby")} == {
            "acme",
            "globex",
        }

    def test_the_failure_is_reported_rather_than_swallowed(
        self, config: AppConfig, store: Database
    ) -> None:
        outcome = run_discovery(
            config,
            store,
            store.start_run("test"),
            discoverers=[ExplodingDiscoverer("dead_one"), WorkingDiscoverer("live", ["acme"])],
        )
        assert len(outcome.errors) == 1
        message = outcome.errors[0]
        assert "dead_one" in message
        assert "Name or service not known" in message, "the real cause must survive"

        per_source = {item.source: item for item in outcome.per_source}
        assert per_source["dead_one"].error
        assert per_source["dead_one"].new_slugs == 0
        assert per_source["live"].error is None
        assert per_source["live"].new_slugs == 1

    def test_a_partly_broken_provider_keeps_what_it_did_read(
        self, config: AppConfig, store: Database
    ) -> None:
        outcome = run_discovery(
            config,
            store,
            store.start_run("test"),
            discoverers=[ReportingFailureDiscoverer()],
        )
        assert outcome.new_slugs == 1
        stats = outcome.per_source[0]
        assert stats.documents_read == 1
        assert stats.documents_failed == 2
        assert "503" in (stats.error or "")

    def test_every_provider_failing_is_survivable(
        self, config: AppConfig, store: Database
    ) -> None:
        outcome = run_discovery(
            config,
            store,
            store.start_run("test"),
            discoverers=[ExplodingDiscoverer("a"), ExplodingDiscoverer("b")],
        )
        assert outcome.new_slugs == 0
        assert len(outcome.errors) == 2

    def test_an_http_error_is_caught_inside_the_provider(self, tmp_path: Path) -> None:
        """A dataset URL returning 500 must not raise out of discover()."""

        class Failing:
            def get(self, url, **kwargs):
                raise requests.HTTPError("500 Server Error")

        provider = PublicDatasetDiscoverer(
            ["https://host.example/list.json"],
            tmp_path,
            user_agent="test",
            session=Failing(),
            robots=None,
        )
        result = provider.discover()
        assert result.candidates == []
        assert result.documents_failed == 1
        assert "500" in (result.error or "")


class TestKnownBoardsStillRefresh:
    def test_verified_boards_refresh_when_discovery_is_entirely_down(
        self, config: AppConfig
    ) -> None:
        """The whole point: yesterday's boards keep working today."""

        database = Database(config.database_path)
        database.initialize()
        try:
            run_pipeline(
                config,
                database=database,
                source=FakeSource({"acme": ["Software Engineer, New Grad"]}),
                discoverers=[WorkingDiscoverer("live", ["acme"])],
            )
            assert database.summary_counts()["active_jobs"] == 1

            source = FakeSource(
                {"acme": ["Software Engineer, New Grad", "Software Engineer I"]}
            )
            summary = run_pipeline(
                config,
                database=database,
                source=source,
                discoverers=[ExplodingDiscoverer("a"), ExplodingDiscoverer("b")],
            )
        finally:
            database.close()

        assert source.requested == ["acme"], "the known board was still contacted"
        assert summary.boards_succeeded == 1
        assert summary.active_jobs == 2, "the new posting was ingested normally"
        assert summary.discovery_errors, "and the discovery outage was still reported"

    def test_one_bad_board_does_not_close_the_others(self, config: AppConfig) -> None:
        database = Database(config.database_path)
        database.initialize()
        try:
            summary = run_pipeline(
                config,
                database=database,
                source=FakeSource({"acme": ["Software Engineer, New Grad"]}),
                discoverers=[WorkingDiscoverer("live", ["acme", "gone"])],
            )
        finally:
            database.close()
        assert summary.boards_succeeded == 1
        assert summary.boards_failed == 1
        assert summary.active_jobs == 1


class TestEmptyDatabaseGuidance:
    def _project(self, tmp_path: Path) -> Path:
        (tmp_path / "config.toml").write_text(
            "[discovery]\n"
            "enabled = true\n"
            'seed_file = ""\n'
            "public_dataset_urls = []\n"
            "[network]\n"
            "min_request_interval = 0.0\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_a_fresh_run_with_no_boards_explains_itself(
        self, tmp_path: Path, capsys
    ) -> None:
        project = self._project(tmp_path)
        code = cli_main(["--config", str(project / "config.toml"), "run", "--no-report"])
        captured = capsys.readouterr()
        assert code == 0, "a discovery outage must not cost the user their dashboard"
        assert "No Ashby boards are known yet" in captured.err
        assert "add-company" in captured.err, "an actionable next step is offered"

    def test_the_message_names_the_failing_sources(self, tmp_path: Path, capsys) -> None:
        from jobfinder.cli import _explain_empty_registry
        from jobfinder.pipeline import RunSummary

        summary = RunSummary(run_id=1, started_at="now")
        summary.discovery_sources = (
            {"source": "public_dataset", "error": "HTTP 503 from host.example"},
            {"source": "wayback", "error": "timed out after 180s"},
        )
        _explain_empty_registry(summary)
        error = capsys.readouterr().err
        assert "public_dataset: HTTP 503" in error
        assert "wayback: timed out" in error
        assert "network problem" in error
        assert "independent" in error, "the user should know one outage is not fatal"

    def test_a_partial_discovery_failure_is_a_note_not_an_error(
        self, config: AppConfig, tmp_path: Path, capsys
    ) -> None:
        """Boards were fetched, so the run succeeded — say so, but mention it."""

        from jobfinder.cli import command_run
        import argparse

        database = Database(config.database_path)
        database.initialize()
        database.close()

        args = argparse.Namespace(
            skip_discovery=False,
            refresh_discovery=False,
            max_companies=None,
            company=None,
            no_report=True,
            serve=False,
            open=False,
        )
        import jobfinder.cli as cli_module
        from jobfinder.pipeline import RunSummary

        summary = RunSummary(run_id=1, started_at="now")
        summary.companies_known = 3
        summary.boards_attempted = 3
        summary.boards_succeeded = 3
        summary.discovery_errors = ("wayback: timed out",)
        monkey = lambda *a, **k: summary  # noqa: E731
        original = cli_module.run_pipeline
        cli_module.run_pipeline = monkey
        try:
            code = command_run(config, args)
        finally:
            cli_module.run_pipeline = original

        error = capsys.readouterr().err
        assert code == 0
        assert "1 discovery source issue" in error
        assert "refreshed normally" in error

    def test_an_empty_registry_is_not_also_told_boards_refreshed_normally(
        self, tmp_path: Path, capsys
    ) -> None:
        """The two messages contradict each other; only one may appear."""

        project = self._project(tmp_path)
        (project / "config.toml").write_text(
            "[discovery]\n"
            "enabled = true\n"
            'seed_file = ""\n'
            'public_dataset_urls = ["https://host.invalid/gone.md"]\n'
            "[network]\n"
            "request_timeout = 1.0\n"
            "retries = 0\n"
            "min_request_interval = 0.0\n",
            encoding="utf-8",
        )
        cli_main(["--config", str(project / "config.toml"), "run", "--no-report"])
        error = capsys.readouterr().err
        assert "No Ashby boards are known yet" in error
        assert "refreshed normally" not in error, (
            "nothing was refreshed, so that note is a lie here"
        )
