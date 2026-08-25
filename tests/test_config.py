from __future__ import annotations

from pathlib import Path

import pytest

from jobfinder.config import load_config


def test_defaults_without_file_resolve_from_requested_parent(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")
    assert config.candidate.graduation_date == "2026-05-31"
    assert config.network.concurrency == 4
    assert config.database_path == tmp_path / "data/jobfinder.sqlite3"


def test_load_override(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[search]\nmin_score = 55\n[network]\nconcurrency = 2\n", encoding="utf-8")
    config = load_config(path)
    assert config.search.min_score == 55
    assert config.network.concurrency == 2
    assert config.database_path.parent == tmp_path / "data"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[search]\nmin_socre = 50\n", encoding="utf-8")
    with pytest.raises(ValueError, match="min_socre"):
        load_config(path)


def test_invalid_concurrency_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[network]\nconcurrency = 99\n", encoding="utf-8")
    with pytest.raises(ValueError, match="concurrency"):
        load_config(path)


class TestPublicDefaults:
    """The shipped config.toml must stay generic and must match the code."""

    def test_the_shipped_config_loads_and_matches_the_code_defaults(self) -> None:
        from jobfinder.config import AppConfig

        shipped = load_config(Path(__file__).resolve().parent.parent / "config.toml")
        built_in = AppConfig()
        for section in ("candidate", "search", "network", "database", "output", "report"):
            assert getattr(shipped, section) == getattr(built_in, section), section
        assert shipped.discovery.use_common_crawl is False
        assert shipped.discovery.use_wayback is False
        assert shipped.discovery.search_endpoints == []
        assert shipped.discovery.respect_robots is True

    def test_the_user_agent_names_the_project_without_impersonating_a_browser(self) -> None:
        from jobfinder.config import AppConfig

        agent = AppConfig().network.user_agent
        assert "SproutJobs" in agent
        assert "Mozilla" not in agent

    def test_no_personal_value_survives_in_the_shipped_config(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "config.toml").read_text(
            encoding="utf-8"
        )
        assert "/Users/" not in text and "/home/" not in text
        assert "home_location" not in text


class TestGraduationCohort:
    """``candidate.graduation_date`` must actually reach the scoring rules."""

    def test_the_config_and_scoring_defaults_cannot_drift(self) -> None:
        from jobfinder.config import DEFAULT_GRADUATION_COHORT as config_default
        from jobfinder.scoring import DEFAULT_GRADUATION_COHORT as scoring_default

        assert config_default == scoring_default

    def test_the_shipped_date_parses_to_the_rule_default(self) -> None:
        from jobfinder.scoring import DEFAULT_GRADUATION_COHORT

        config = load_config(Path(__file__).resolve().parent.parent / "config.toml")
        assert config.graduation_cohort == DEFAULT_GRADUATION_COHORT

    def test_a_configured_date_is_parsed(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('[candidate]\ngraduation_date = "2028-12-15"\n', encoding="utf-8")
        assert load_config(path).graduation_cohort == (2028, 12)

    def test_a_malformed_date_is_rejected_loudly(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('[candidate]\ngraduation_date = "next spring"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="graduation_date"):
            load_config(path)

    def test_the_cohort_changes_a_graduation_scoped_verdict(self) -> None:
        """The setting is wired, not decorative."""

        from jobfinder.models import NormalizedJob
        from jobfinder.scoring import analyze_normalized_job

        def analyze(cohort: tuple[int, int]):
            job = NormalizedJob(
                source="ashby",
                source_company_slug="acme",
                source_job_id="1",
                company_name="Acme",
                title="Software Engineer",
                location="New York, NY",
                description_text=(
                    "We are hiring for our early career program. "
                    "You must be graduating in 2028 to be eligible."
                ),
                content_hash="hash-1",
            )
            return analyze_normalized_job(job, attach=False, graduation_cohort=cohort)

        matching = analyze((2028, 5))
        mismatched = analyze((2026, 5))
        assert matching.score != mismatched.score
        assert "incompatible_graduation_window" in mismatched.hard_blockers
        assert "incompatible_graduation_window" not in matching.hard_blockers

    def test_a_posting_that_says_nothing_is_unaffected_by_the_cohort(self) -> None:
        from jobfinder.models import NormalizedJob
        from jobfinder.scoring import analyze_normalized_job

        def analyze(cohort: tuple[int, int]):
            job = NormalizedJob(
                source="ashby",
                source_company_slug="acme",
                source_job_id="1",
                company_name="Acme",
                title="Software Engineer, New Grad",
                location="New York, NY",
                description_text="0-2 years of experience. BS in Computer Science.",
                content_hash="hash-1",
            )
            return analyze_normalized_job(job, attach=False, graduation_cohort=cohort)

        assert analyze((2026, 5)).as_dict() == analyze((2031, 9)).as_dict()
