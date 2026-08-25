"""Tests for the command surface a user actually types."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobfinder import cli
from jobfinder.config import load_config
from jobfinder.db import Database


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "config.toml").write_text(
        "[discovery]\nenabled = false\npublic_dataset_urls = []\n"
        "[network]\nmin_request_interval = 0.0\n",
        encoding="utf-8",
    )
    return tmp_path


def run_cli(project: Path, *args: str) -> int:
    return cli.main(["--config", str(project / "config.toml"), *args])


class TestArgumentHandling:
    def test_run_is_the_default_command(self) -> None:
        parser = cli.build_parser()
        assert parser.parse_args(["run"]).command == "run"
        assert parser.parse_args([]).command is None

    def test_global_options_precede_the_command(self) -> None:
        args = cli.build_parser().parse_args(["--verbose", "--config", "x.toml", "run"])
        assert args.verbose and args.config == "x.toml" and args.command == "run"

    def test_documented_run_flags_exist(self) -> None:
        args = cli.build_parser().parse_args(
            ["run", "--skip-discovery", "--refresh-discovery", "--max-companies", "5"]
        )
        assert args.skip_discovery and args.refresh_discovery and args.max_companies == 5

    def test_an_invalid_config_is_reported_not_raised(self, tmp_path: Path) -> None:
        bad = tmp_path / "config.toml"
        bad.write_text("[search]\nmin_socre = 1\n", encoding="utf-8")
        assert cli.main(["--config", str(bad), "report"]) == 2


class TestOfflineCommands:
    def test_run_without_companies_succeeds_and_writes_reports(
        self, project: Path, capsys
    ) -> None:
        assert run_cli(project, "run") == 0

        output = capsys.readouterr().out
        assert "SPROUTJOBS" in output
        assert (project / "output" / "index.html").exists()
        assert (project / "output" / "jobs.csv").exists()
        assert (project / "data" / "jobfinder.sqlite3").exists()

    def test_report_rebuilds_from_the_database_without_network_access(
        self, project: Path, capsys, monkeypatch
    ) -> None:
        run_cli(project, "run")

        def explode(*args, **kwargs):  # pragma: no cover - only runs on a failure
            raise AssertionError("report must not make network requests")

        monkeypatch.setattr("jobfinder.pipeline.build_source", explode)
        assert run_cli(project, "report") == 0

        output = capsys.readouterr().out
        assert "Dashboard:" in output and "CSV:" in output

    def test_companies_lists_the_registry(self, project: Path, capsys) -> None:
        config = load_config(project / "config.toml")
        database = Database(config.database_path)
        database.initialize()
        database.record_company_success("acme", display_name="Acme", job_count=3)
        database.close()

        assert run_cli(project, "companies") == 0
        assert "acme" in capsys.readouterr().out

    def test_companies_is_helpful_when_empty(self, project: Path, capsys) -> None:
        assert run_cli(project, "companies") == 0
        assert "No companies yet" in capsys.readouterr().out

    def test_open_static_explains_itself_when_no_export_exists(
        self, project: Path, capsys
    ) -> None:
        assert run_cli(project, "open", "--static") == 1
        assert "Run 'python -m jobfinder run' first" in capsys.readouterr().err

    def test_open_static_uses_a_file_url(self, project: Path, monkeypatch, capsys) -> None:
        """The capped HTML export is still reachable, but only on request."""

        run_cli(project, "run")
        opened: list[str] = []
        monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)

        assert run_cli(project, "open", "--static") == 0
        assert opened and opened[0].startswith("file://")
        assert opened[0].endswith("index.html")

    def test_open_targets_the_live_dashboard_by_default(
        self, project: Path, monkeypatch, capsys
    ) -> None:
        """Requirement 16: `open` reaches the server, not a capped file."""

        run_cli(project, "run")
        opened: list[str] = []
        monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)
        # Pretend a dashboard is already listening: `open` should attach to it
        # rather than starting a second copy, and must never block.
        monkeypatch.setattr(cli.server_module, "is_running", lambda host, port: True)

        assert run_cli(project, "open") == 0
        assert opened and opened[0].startswith("http://127.0.0.1:")
        assert "already running" in capsys.readouterr().out

    def test_serve_attaches_to_an_existing_dashboard(
        self, project: Path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(cli.server_module, "is_running", lambda host, port: True)
        assert run_cli(project, "serve", "--port", "8799") == 0
        assert "8799" in capsys.readouterr().out

    def test_add_url_rejects_a_non_ashby_link_without_network_access(
        self, project: Path, capsys, monkeypatch
    ) -> None:
        def explode(*args, **kwargs):  # pragma: no cover - only runs on a failure
            raise AssertionError("an invalid URL must be rejected before any request")

        monkeypatch.setattr("jobfinder.pipeline.build_source", explode)
        assert run_cli(project, "add-url", "https://example.com/acme") == 1
        assert "rejected" in capsys.readouterr().err


class TestOverrides:
    def test_min_score_override_is_validated(self, project: Path, capsys) -> None:
        assert run_cli(project, "report", "--min-score", "150") == 2
        assert "Configuration error" in capsys.readouterr().err

    def test_min_score_override_reaches_the_config(self, project: Path) -> None:
        config = load_config(project / "config.toml")
        args = cli.build_parser().parse_args(["report", "--min-score", "70"])
        cli._apply_overrides(config, args)
        assert config.search.min_score == 70

    def test_include_non_software_override(self, project: Path) -> None:
        config = load_config(project / "config.toml")
        assert config.report.include_non_software is False
        args = cli.build_parser().parse_args(["run", "--include-non-software"])
        cli._apply_overrides(config, args)
        assert config.report.include_non_software is True


class TestDiscoveryAndStatsCommands:
    def test_discovery_report_runs_on_an_empty_database(
        self, project: Path, capsys
    ) -> None:
        assert run_cli(project, "discovery-report") == 0
        assert "Run 'python -m jobfinder run'" in capsys.readouterr().out

    def test_discovery_report_shows_per_source_and_overlap(
        self, project: Path, capsys
    ) -> None:
        config = load_config(project / "config.toml")
        database = Database(config.database_path)
        database.initialize()
        try:
            database.record_slug_sources(
                [
                    ("notion", "common_crawl", "cc"),
                    ("linear", "common_crawl", "cc"),
                    ("notion", "wayback", "wb"),
                ]
            )
            database.record_company_success("notion", display_name="Notion", job_count=2)
        finally:
            database.close()

        assert run_cli(project, "discovery-report") == 0
        output = capsys.readouterr().out
        assert "common_crawl" in output and "wayback" in output
        assert "OVERLAP BETWEEN SOURCES" in output
        assert "1 shared slug(s)" in output
        assert "verified" in output

    def test_stats_reports_geography_and_experience(self, project: Path, capsys) -> None:
        from jobfinder.models import NormalizedJob
        from jobfinder.scoring import analyze_normalized_job

        config = load_config(project / "config.toml")
        database = Database(config.database_path)
        database.initialize()
        try:
            run_id = database.start_run("test")
            jobs = []
            for index, (title, location, description) in enumerate(
                [
                    ("Software Engineer, New Grad", "Austin, TX", "0-2 years."),
                    ("Software Engineer", "New York, NY", "Build services."),
                ]
            ):
                job = NormalizedJob(
                    source="ashby",
                    source_company_slug="acme",
                    source_job_id=f"job-{index}",
                    company_name="Acme",
                    title=title,
                    location=location,
                    location_context=f"{location}, United States",
                    description_text=description,
                    content_hash=f"hash-{index}",
                )
                analyze_normalized_job(job)
                jobs.append(job)
            database.ingest_company_jobs("acme", jobs, run_id)
        finally:
            database.close()

        assert run_cli(project, "stats") == 0
        output = capsys.readouterr().out
        assert "GEOGRAPHY" in output and "EXPERIENCE EVIDENCE" in output
        assert "Other US" in output and "NYC" in output
        assert "Not stated" in output
        assert "unknown is not the same as unsuitable" in output
