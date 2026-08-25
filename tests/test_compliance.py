"""The technical compliance audit must actually audit something.

A safeguard check that passes unconditionally is worse than no check at all, so
every test here proves a check *fails* when the property it guards is violated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobfinder.cli import main as cli_main
from jobfinder.compliance import (
    FAIL,
    PASS,
    WARN,
    AuditReport,
    format_report,
    run_audit,
)
from jobfinder.config import AppConfig, load_config


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def config() -> AppConfig:
    return load_config(ROOT / "config.toml")


def check(report: AuditReport, fragment: str):
    """The one check whose name contains ``fragment``."""

    matches = [item for item in report.checks if fragment in item.name]
    assert len(matches) == 1, f"{fragment!r} matched {[m.name for m in matches]}"
    return matches[0]


class TestShippedCheckoutPasses:
    def test_the_repository_as_shipped_has_no_failures(self, config: AppConfig) -> None:
        report = run_audit(config, project_root=ROOT)
        assert report.failures == [], [item.as_dict() for item in report.failures]
        assert report.ok is True
        assert report.exit_code == 0

    def test_every_check_reports_a_known_status_and_an_explanation(
        self, config: AppConfig
    ) -> None:
        report = run_audit(config, project_root=ROOT)
        assert report.checks, "the audit ran no checks at all"
        for item in report.checks:
            assert item.status in {PASS, WARN, FAIL}
            assert item.detail.strip(), f"{item.name} explains nothing"

    def test_the_rendered_report_refuses_to_claim_legality(
        self, config: AppConfig
    ) -> None:
        text = format_report(run_audit(config, project_root=ROOT))
        assert "not legal advice" in text
        lowered = text.lower()
        for forbidden in ("100% legal", "fully compliant", "approved by ashby"):
            assert forbidden not in lowered


class TestSourcePolicy:
    def test_enabling_common_crawl_is_a_failure(self, config: AppConfig) -> None:
        config.discovery.use_common_crawl = True
        report = run_audit(config, project_root=ROOT)
        result = check(report, "forbidden discovery source")
        assert result.status == FAIL
        assert "use_common_crawl" in result.detail
        assert report.exit_code == 1

    def test_turning_off_robots_is_a_failure(self, config: AppConfig) -> None:
        config.discovery.respect_robots = False
        result = check(run_audit(config, project_root=ROOT), "robots.txt is consulted")
        assert result.status == FAIL

    def test_a_configured_search_endpoint_warns_but_does_not_fail(
        self, config: AppConfig
    ) -> None:
        config.discovery.search_endpoints = ["https://search.example/?q={query}"]
        report = run_audit(config, project_root=ROOT)
        assert check(report, "search endpoints").status == WARN
        assert report.ok is True, "an unresolved question must not block anything"

    def test_wayback_is_enableable_without_failing_the_audit(
        self, config: AppConfig
    ) -> None:
        """It is off for cost, not policy, so turning it on must stay legitimate."""

        config.discovery.use_wayback = True
        report = run_audit(config, project_root=ROOT)
        assert report.failures == [], [item.as_dict() for item in report.failures]

    def test_an_undocumented_enabled_source_fails(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        (tmp_path / "SOURCES.md").write_text("# Sources\n\nNothing.\n", encoding="utf-8")
        (tmp_path / "jobfinder").mkdir()
        result = check(
            run_audit(config, project_root=tmp_path), "documented in SOURCES.md"
        )
        assert result.status == FAIL


class TestNetworkLimits:
    def test_unbounded_concurrency_fails(self, config: AppConfig) -> None:
        config.network.concurrency = 64
        result = check(run_audit(config, project_root=ROOT), "concurrency is bounded")
        assert result.status == FAIL
        assert "64" in result.detail

    def test_the_config_validator_refuses_high_concurrency_first(self) -> None:
        """The audit is a backstop; the configuration cannot get there normally."""

        from jobfinder.config import _validate

        config = AppConfig()
        config.network.concurrency = 64
        with pytest.raises(ValueError, match="concurrency"):
            _validate(config)

    def test_a_missing_timeout_fails(self, config: AppConfig) -> None:
        config.network.request_timeout = 0
        assert check(run_audit(config, project_root=ROOT), "timeout").status == FAIL

    def test_retrying_without_backoff_fails(self, config: AppConfig) -> None:
        config.network.backoff_seconds = 0
        assert check(run_audit(config, project_root=ROOT), "backoff").status == FAIL

    def test_no_rate_limit_warns(self, config: AppConfig) -> None:
        config.network.min_request_interval = 0
        assert check(run_audit(config, project_root=ROOT), "rate limited").status == WARN

    def test_a_browser_impersonating_user_agent_fails(self, config: AppConfig) -> None:
        config.network.user_agent = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36"
        result = check(run_audit(config, project_root=ROOT), "User-Agent")
        assert result.status == FAIL
        assert "impersonates" in result.detail

    def test_an_empty_user_agent_fails(self, config: AppConfig) -> None:
        config.network.user_agent = "  "
        assert check(run_audit(config, project_root=ROOT), "User-Agent").status == FAIL

    def test_a_contactable_user_agent_passes(self, config: AppConfig) -> None:
        config.network.user_agent = "SproutJobs/1.0 (+https://example.invalid/repo)"
        assert check(run_audit(config, project_root=ROOT), "User-Agent").status == PASS

    def test_the_shipped_user_agent_is_already_contactable(
        self, config: AppConfig
    ) -> None:
        """The default carries the project URL, so it needs no warning."""

        result = check(run_audit(config, project_root=ROOT), "User-Agent")
        assert result.status == PASS
        assert "github.com/yashtamhane/SproutJobs" in result.detail
        assert result.remedy == ""

    def test_a_user_agent_with_no_way_to_reach_anyone_warns(
        self, config: AppConfig
    ) -> None:
        config.network.user_agent = "SproutJobs/1.0"
        result = check(run_audit(config, project_root=ROOT), "User-Agent")
        assert result.status == WARN
        assert result.remedy


class TestCodeProperties:
    """These read the real package, so they fail if the code regresses."""

    def test_429_and_retry_after_are_implemented(self, config: AppConfig) -> None:
        assert check(run_audit(config, project_root=ROOT), "429").status == PASS

    def test_the_dashboard_default_is_loopback(self, config: AppConfig) -> None:
        assert check(run_audit(config, project_root=ROOT), "loopback").status == PASS

    def test_nothing_writes_to_a_remote_server(self, config: AppConfig) -> None:
        result = check(run_audit(config, project_root=ROOT), "no application")
        assert result.status == PASS

    def test_no_applicant_credential_is_collected(self, config: AppConfig) -> None:
        assert check(run_audit(config, project_root=ROOT), "credential is collected").status == PASS

    def test_scoring_determinism_is_verified(self, config: AppConfig) -> None:
        assert check(run_audit(config, project_root=ROOT), "deterministic").status == PASS

    def test_no_paid_client_or_browser_driver(self, config: AppConfig) -> None:
        report = run_audit(config, project_root=ROOT)
        assert check(report, "paid or model client").status == PASS
        assert check(report, "browser automation").status == PASS
        assert check(report, "CAPTCHA").status == PASS

    def test_the_scanner_ignores_module_names_that_are_only_mentioned(self) -> None:
        """jobfinder/compliance.py lists 'selenium' as a string and must not self-report."""

        from jobfinder.compliance import _imported_modules

        found = _imported_modules(ROOT / "jobfinder" / "compliance.py")
        assert "selenium" not in found
        assert "openai" not in found
        assert "ast" in found, "the parser should still see genuine imports"


class TestRepositoryHygiene:
    def test_the_shipped_gitignore_covers_runtime_data(self, config: AppConfig) -> None:
        assert check(run_audit(config, project_root=ROOT), "git-ignored").status == PASS

    def test_a_missing_gitignore_fails(self, config: AppConfig, tmp_path: Path) -> None:
        (tmp_path / "jobfinder").mkdir()
        result = check(run_audit(config, project_root=tmp_path), "git-ignored")
        assert result.status == FAIL

    def test_an_incomplete_gitignore_fails(self, config: AppConfig, tmp_path: Path) -> None:
        (tmp_path / "jobfinder").mkdir()
        (tmp_path / ".gitignore").write_text("__pycache__/\n.venv/\n", encoding="utf-8")
        result = check(run_audit(config, project_root=tmp_path), "git-ignored")
        assert result.status == FAIL
        assert "reports" in result.detail or "database" in result.detail

    def test_a_non_git_directory_warns_rather_than_failing(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        (tmp_path / "jobfinder").mkdir()
        result = check(run_audit(config, project_root=tmp_path), "tracked by Git")
        assert result.status == WARN
        assert "git init" in result.remedy

    def test_a_database_outside_the_project_warns(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        config.database.path = str(tmp_path / "elsewhere.sqlite3")
        result = check(run_audit(config, project_root=ROOT), "stored locally")
        assert result.status == WARN


class TestCommandLine:
    def test_the_command_exits_zero_on_the_shipped_checkout(self, capsys) -> None:
        code = cli_main(["--config", str(ROOT / "config.toml"), "compliance"])
        output = capsys.readouterr().out
        assert code == 0
        assert "SPROUTJOBS TECHNICAL COMPLIANCE AUDIT" in output
        assert "not legal advice" in output

    def test_json_output_is_machine_readable(self, capsys) -> None:
        code = cli_main(["--config", str(ROOT / "config.toml"), "compliance", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["ok"] is True
        assert payload["failed"] == 0
        assert payload["passed"] > 10
        assert {"name", "status", "detail"} <= set(payload["checks"][0])
