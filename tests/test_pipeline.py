"""End-to-end pipeline tests driven by a fake source and fake discovery.

These cover the whole normal-use path — discover, verify, ingest, score, report —
without any network access.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from jobfinder.config import load_config
from jobfinder.db import Database
from jobfinder.discovery.base import DiscoveryCandidate, DiscoveryResult
from jobfinder.models import BoardFetchResult, BoardValidation
from jobfinder.normalize import normalize_ashby_job, utc_now
from jobfinder.pipeline import add_companies, format_summary, run_pipeline, select_companies
from jobfinder.sources.base import BoardNotFoundError, SourceHTTPError


NEW_GRAD_DESCRIPTION = (
    "<p>We welcome recent graduates.</p><ul><li>0-2 years of experience</li>"
    "<li>Bachelor's degree in Computer Science</li></ul>"
    "<p>You will build and maintain backend services in Python.</p>"
)
SENIOR_DESCRIPTION = (
    "<p>Requirements: 8+ years of professional software engineering experience.</p>"
)


def raw_job(job_id: str, title: str, description: str = NEW_GRAD_DESCRIPTION, **overrides) -> dict:
    values = {
        "id": job_id,
        "title": title,
        "department": "Engineering",
        "team": "Platform",
        "location": "New York, NY",
        "workplaceType": "Hybrid",
        "employmentType": "FullTime",
        "publishedAt": "2026-08-18T14:30:00.000Z",
        "isListed": True,
        "descriptionHtml": description,
        "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}",
        "applyUrl": f"https://jobs.ashbyhq.com/acme/{job_id}/application",
        "address": {"postalAddress": {"addressLocality": "New York", "addressCountry": "USA"}},
    }
    values.update(overrides)
    return values


class FakeSource:
    """A stand-in adapter whose boards and failures the test controls."""

    source_name = "ashby"

    def __init__(self, boards: dict[str, list[dict]], failures: dict[str, Exception] | None = None):
        self.boards = boards
        self.failures = failures or {}
        self.fetched: list[str] = []

    def fetch_company_jobs(self, slug: str) -> BoardFetchResult:
        self.fetched.append(slug)
        if slug in self.failures:
            raise self.failures[slug]
        if slug not in self.boards:
            raise BoardNotFoundError(f"no board {slug}", status_code=404)
        jobs = tuple(
            normalize_ashby_job(item, slug, slug.capitalize()) for item in self.boards[slug]
        )
        return BoardFetchResult(
            source="ashby",
            slug=slug,
            display_name=slug.capitalize(),
            board_url=f"https://jobs.ashbyhq.com/{slug}",
            fetched_at=utc_now(),
            jobs=jobs,
        )

    def validate_company(self, slug: str) -> BoardValidation:
        try:
            board = self.fetch_company_jobs(slug)
        except (BoardNotFoundError, SourceHTTPError) as exc:
            return BoardValidation(slug=slug, valid=False, error=str(exc))
        return BoardValidation(
            slug=slug,
            valid=True,
            display_name=board.display_name,
            job_count=len(board.jobs),
            board_url=board.board_url,
        )

    def close(self) -> None:
        return None


class FakeDiscoverer:
    name = "fake_dataset"

    def __init__(self, slugs, evidence="https://example.test/list.md"):
        self.slugs = list(slugs)
        self.evidence = evidence
        self.calls = 0

    def discover(self, force: bool = False) -> DiscoveryResult:
        self.calls += 1
        return DiscoveryResult(
            source=self.name,
            candidates=[
                DiscoveryCandidate(slug=slug, source=self.name, evidence=self.evidence)
                for slug in self.slugs
            ],
            fetched=True,
        )


@pytest.fixture()
def config(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        "[network]\nconcurrency = 2\nmin_request_interval = 0.0\n"
        "[report]\ninclude_non_software = true\n",
        encoding="utf-8",
    )
    return load_config(tmp_path / "config.toml")


def jobs_by_id(config) -> dict[str, dict]:
    database = Database(config.database_path)
    try:
        return {row["source_job_id"]: row for row in database.list_jobs(active=None)}
    finally:
        database.close()


class TestFullRun:
    def test_discovery_verification_ingestion_scoring_and_reports(self, config) -> None:
        source = FakeSource(
            {
                "acme": [
                    raw_job("j1", "Software Engineer, New Grad"),
                    raw_job("j2", "Senior Software Engineer", SENIOR_DESCRIPTION),
                    raw_job("j3", "Account Executive", "<p>Close deals and hit quota.</p>"),
                ]
            }
        )
        discoverer = FakeDiscoverer(["acme", "not-a-real-board"])

        summary = run_pipeline(config, source=source, discoverers=[discoverer])

        # Discovery registered both candidates; only the real one survived contact.
        assert summary.discovery_new_slugs == 2
        assert summary.companies_known == 2
        assert summary.companies_verified == 1
        assert summary.boards_succeeded == 1
        assert summary.boards_failed == 1

        assert summary.active_jobs == 3
        assert summary.software_jobs == 2
        assert summary.strong_matches == 1
        assert summary.new_strong_matches == 1

        rows = jobs_by_id(config)
        assert rows["j1"]["match_category"] == "strong_match"
        assert rows["j2"]["match_category"] == "likely_ineligible"
        assert rows["j3"]["is_software"] is False
        assert json.loads(rows["j1"]["score_reasons_json"])

        assert Path(summary.dashboard_path).exists()
        assert Path(summary.csv_path).exists()
        page = Path(summary.dashboard_path).read_text(encoding="utf-8")
        assert "Software Engineer, New Grad" in page
        assert "https://jobs.ashbyhq.com/acme/j1/application" in page

    def test_apply_links_come_from_the_source_and_reach_the_reports(self, config) -> None:
        source = FakeSource({"acme": [raw_job("j1", "Software Engineer, New Grad")]})
        summary = run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        with Path(summary.csv_path).open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["apply_url"] == "https://jobs.ashbyhq.com/acme/j1/application"
        assert row["job_url"] == "https://jobs.ashbyhq.com/acme/j1"
        assert row["category"] == "strong_match"

    def test_a_second_run_adds_no_duplicates_and_keeps_first_seen(self, config) -> None:
        source = FakeSource({"acme": [raw_job("j1", "Software Engineer, New Grad")]})
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        first = jobs_by_id(config)["j1"]

        second_summary = run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        second = jobs_by_id(config)["j1"]

        assert len(jobs_by_id(config)) == 1
        assert second["first_seen_at"] == first["first_seen_at"]
        assert second["last_seen_at"] >= first["last_seen_at"]
        assert second["new_since_previous_run"] is False
        assert second_summary.jobs_inserted == 0
        assert second_summary.new_strong_matches == 0

    def test_a_job_removed_from_the_board_is_closed_and_kept(self, config) -> None:
        source = FakeSource(
            {"acme": [raw_job("j1", "Software Engineer, New Grad"), raw_job("j2", "Backend Engineer")]}
        )
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        source.boards["acme"] = [raw_job("j1", "Software Engineer, New Grad")]
        summary = run_pipeline(config, source=source, discoverers=[])

        rows = jobs_by_id(config)
        assert summary.jobs_closed == 1
        assert rows["j1"]["active"] is True
        assert rows["j2"]["active"] is False
        assert rows["j2"]["closed_at"]

    def test_a_failing_board_never_closes_its_jobs(self, config) -> None:
        source = FakeSource({"acme": [raw_job("j1", "Software Engineer, New Grad")]})
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        source.failures["acme"] = SourceHTTPError("HTTP 503", status_code=503)
        summary = run_pipeline(config, source=source, discoverers=[])

        assert summary.boards_failed == 1
        assert summary.jobs_closed == 0
        assert jobs_by_id(config)["j1"]["active"] is True
        assert summary.failures[0][0] == "acme"

    def test_one_broken_board_does_not_abort_the_run(self, config) -> None:
        source = FakeSource(
            {"acme": [raw_job("j1", "Software Engineer, New Grad")], "globex": [raw_job("g1", "Backend Engineer")]},
            failures={"globex": SourceHTTPError("boom", status_code=500)},
        )
        summary = run_pipeline(
            config, source=source, discoverers=[FakeDiscoverer(["acme", "globex"])]
        )

        assert summary.boards_succeeded == 1
        assert summary.boards_failed == 1
        assert "j1" in jobs_by_id(config)

    def test_run_history_records_stats_for_auditing(self, config) -> None:
        source = FakeSource({"acme": [raw_job("j1", "Software Engineer, New Grad")]})
        summary = run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        database = Database(config.database_path)
        try:
            run = database.get_run(summary.run_id)
        finally:
            database.close()
        assert run["status"] == "success"
        assert run["completed_at"]
        assert run["stats"]["boards_succeeded"] == 1


class TestSelectionAndSeeds:
    def test_discovery_can_be_skipped(self, config) -> None:
        source = FakeSource({"acme": [raw_job("j1", "Software Engineer, New Grad")]})
        discoverer = FakeDiscoverer(["acme"])
        run_pipeline(config, source=source, discoverers=[discoverer], skip_discovery=True)
        assert discoverer.calls == 0
        assert source.fetched == []

    def test_max_companies_limits_network_contact(self, config) -> None:
        source = FakeSource({name: [raw_job(f"{name}-1", "Backend Engineer")] for name in ("a", "b", "c")})
        run_pipeline(
            config, source=source, discoverers=[FakeDiscoverer(["a", "b", "c"])], max_companies=2
        )
        assert len(source.fetched) == 2

    def test_a_dead_board_is_not_retried_until_the_cool_off_elapses(self, config) -> None:
        source = FakeSource({"acme": [raw_job("j1", "Backend Engineer")]})
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme", "ghost"])])

        database = Database(config.database_path)
        try:
            company = database.get_company("ghost")
            assert company["active"] is False
            selected = {item["slug"] for item in select_companies(config, database)}
        finally:
            database.close()
        assert "acme" in selected
        assert "ghost" not in selected

    def test_a_user_seed_is_verified_before_it_is_trusted(self, config) -> None:
        source = FakeSource({"acme": [raw_job("j1", "Backend Engineer")]})
        results = add_companies(config, ["https://jobs.ashbyhq.com/acme", "ghost"], source=source)

        assert results[0]["verified"] is True
        assert results[0]["slug"] == "acme"
        assert results[1]["verified"] is False

        database = Database(config.database_path)
        try:
            assert database.get_company("acme")["active"] is True
            assert database.get_company("ghost")["active"] is False
        finally:
            database.close()

    def test_a_non_ashby_url_is_rejected_without_contacting_anything(self, config) -> None:
        source = FakeSource({})
        results = add_companies(config, ["https://example.com/acme"], source=source)
        assert results[0]["verified"] is False
        assert source.fetched == []


class TestSummaryOutput:
    def test_the_summary_prints_measured_values_only(self, config) -> None:
        source = FakeSource({"acme": [raw_job("j1", "Software Engineer, New Grad")]})
        summary = run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        text = format_summary(summary)

        assert "SPROUTJOBS" in text
        assert f"{summary.active_jobs:,}" in text
        assert str(config.database_path) in text
        assert summary.dashboard_path in text
        assert summary.csv_path in text

    def test_failures_are_surfaced_rather_than_swallowed(self, config) -> None:
        source = FakeSource({}, failures={"acme": SourceHTTPError("HTTP 503", status_code=503)})
        summary = run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        text = format_summary(summary)

        assert "board(s) failed this run" in text
        assert "acme" in text


FABRIKAM_DESCRIPTION = (
    "<p>Fabrikam builds planning software for industrial manufacturers. You will "
    "design, build and maintain distributed backend services in Python and Go, own "
    "features end to end, and work directly with the founders on the core platform "
    "that everything else depends on.</p>"
)


def fabrikam_board(count: int = 6) -> list[dict]:
    """A synthetic board shaped like a real one: one opening under six source ids."""

    return [
        raw_job(
            f"fabrikam-{index}",
            "Software Engineer, Core Platform (All Levels)",
            FABRIKAM_DESCRIPTION,
            location="HQ (San Francisco, CA)",
            workplaceType="OnSite",
            publishedAt=f"2026-08-20T19:40:5{index}.000Z",
            jobUrl=f"https://jobs.ashbyhq.com/acme/fabrikam-{index}",
            applyUrl=f"https://jobs.ashbyhq.com/acme/fabrikam-{index}/application",
        )
        for index in range(count)
    ]


class TestDuplicateOpportunities:
    """The duplicated-opening case, end to end through the real pipeline."""

    def _rows(self, path: Path) -> list[dict]:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_repeated_postings_appear_once_in_both_reports(self, config) -> None:
        source = FakeSource({"acme": fabrikam_board()})
        summary = run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        # Every source record is still stored.
        assert len(jobs_by_id(config)) == 6
        assert summary.active_jobs == 6

        # But the reports show one opportunity, and they agree with each other.
        exported = self._rows(Path(summary.csv_path))
        assert len(exported) == 1
        assert exported[0]["duplicate_count"] == "6"
        assert len(exported[0]["duplicate_source_ids"].split("; ")) == 5

        page = Path(summary.dashboard_path).read_text(encoding="utf-8")
        assert page.count('class="job-card"') == 1
        assert "6× posted" in page
        assert summary.collapsed_duplicates == 5

    def test_the_displayed_apply_link_belongs_to_the_shown_record(self, config) -> None:
        source = FakeSource({"acme": fabrikam_board()})
        summary = run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        exported = self._rows(Path(summary.csv_path))[0]
        assert exported["apply_url"].endswith("/application")
        assert exported["apply_url"].startswith("https://jobs.ashbyhq.com/acme/")
        page = Path(summary.dashboard_path).read_text(encoding="utf-8")
        assert exported["apply_url"] in page

    def test_repeated_runs_add_no_further_display_duplicates(self, config) -> None:
        """(d) Re-running must not multiply what the reader sees."""

        source = FakeSource({"acme": fabrikam_board()})
        discoverer = FakeDiscoverer(["acme"])
        first = run_pipeline(config, source=source, discoverers=[discoverer])
        second = run_pipeline(config, source=source, discoverers=[])
        third = run_pipeline(config, source=source, discoverers=[])

        assert first.csv_jobs == second.csv_jobs == third.csv_jobs == 1
        assert len(jobs_by_id(config)) == 6, "source records stay put"
        assert len(self._rows(Path(third.csv_path))) == 1
        page = Path(third.dashboard_path).read_text(encoding="utf-8")
        assert page.count('class="job-card"') == 1

    def test_a_board_of_genuinely_distinct_roles_is_not_collapsed(self, config) -> None:
        source = FakeSource(
            {
                "acme": [
                    raw_job("a", "Software Engineer, New Grad", location="New York, NY"),
                    raw_job("b", "Software Engineer, New Grad", location="Austin, TX"),
                    raw_job("c", "Senior Software Engineer", SENIOR_DESCRIPTION),
                ]
            }
        )
        summary = run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        assert summary.csv_jobs == 3
        assert summary.collapsed_duplicates == 0

    def test_a_closed_duplicate_does_not_replace_the_active_one(self, config) -> None:
        source = FakeSource({"acme": fabrikam_board()})
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        # The board drops all but one id; the rest close but stay in the database.
        source.boards["acme"] = fabrikam_board()[:1]
        summary = run_pipeline(config, source=source, discoverers=[])

        assert summary.jobs_closed == 5
        exported = self._rows(Path(summary.csv_path))
        assert len(exported) == 1
        assert exported[0]["active"] == "true"
        assert exported[0]["source_company_slug"] == "acme"

    def test_the_canonical_key_is_stored_for_debugging(self, config) -> None:
        source = FakeSource({"acme": fabrikam_board()})
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])

        database = Database(config.database_path)
        try:
            connection = database.connect()
            keys = [
                row["canonical_key"]
                for row in connection.execute("SELECT canonical_key FROM jobs").fetchall()
            ]
            connection.close()
        finally:
            database.close()

        assert len(keys) == 6
        assert len(set(keys)) == 1, "identical postings share one canonical key"
        assert all(key for key in keys)


class TestUnitedStatesWideIngestion:
    """Collection is nationwide; only NYC/NJ *preference* was ever the point."""

    def test_jobs_outside_nyc_and_nj_are_ingested_and_reported(self, config) -> None:
        source = FakeSource(
            {
                "acme": [
                    raw_job(
                        "austin",
                        "Software Engineer, New Grad",
                        location="Austin, TX",
                        address={
                            "postalAddress": {
                                "addressLocality": "Austin",
                                "addressRegion": "Texas",
                                "addressCountry": "United States",
                            }
                        },
                    ),
                    raw_job(
                        "seattle",
                        "Associate Software Engineer",
                        location="Seattle, WA",
                        address={
                            "postalAddress": {
                                "addressLocality": "Seattle",
                                "addressRegion": "Washington",
                                "addressCountry": "United States",
                            }
                        },
                    ),
                ]
            }
        )
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        stored = jobs_by_id(config)
        assert set(stored) == {"austin", "seattle"}
        for row in stored.values():
            assert row["geo_tags"] == ["other_us"], row["title"]
            assert row["match_category"] == "strong_match"

        csv_rows = list(csv.DictReader(config.csv_path.open(encoding="utf-8", newline="")))
        exported = {row["title"] for row in csv_rows}
        assert "Software Engineer, New Grad" in exported
        assert "Associate Software Engineer" in exported

    def test_a_multi_city_posting_carries_several_geography_tags(self, config) -> None:
        source = FakeSource(
            {
                "acme": [
                    raw_job(
                        "multi",
                        "Software Engineer I",
                        location="New York, NY",
                        secondaryLocations=[
                            {"location": {"name": "Austin, TX"}},
                            {"location": {"name": "Jersey City, NJ"}},
                        ],
                    )
                ]
            }
        )
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        row = jobs_by_id(config)["multi"]
        assert set(row["geo_tags"]) == {"nyc", "new_jersey", "other_us"}

    def test_a_non_us_job_is_stored_but_kept_out_of_the_reports(self, config) -> None:
        source = FakeSource(
            {
                "acme": [
                    raw_job(
                        "london",
                        "Graduate Software Engineer",
                        location="London",
                        address={
                            "postalAddress": {
                                "addressLocality": "London",
                                "addressCountry": "United Kingdom",
                            }
                        },
                    ),
                    raw_job("nyc", "Software Engineer, New Grad"),
                ]
            }
        )
        config.report.include_non_us = False
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        stored = jobs_by_id(config)
        assert set(stored) == {"london", "nyc"}, "history keeps everything it saw"
        csv_rows = list(csv.DictReader(config.csv_path.open(encoding="utf-8", newline="")))
        assert {row["title"] for row in csv_rows} == {"Software Engineer, New Grad"}

    def test_unclear_geography_is_kept_rather_than_dropped(self, config) -> None:
        """'Remote' with no country is unknown, and unknown is not excluded."""

        source = FakeSource(
            {
                "acme": [
                    raw_job(
                        "vague",
                        "Software Engineer, New Grad",
                        location="Remote",
                        address=None,
                    )
                ]
            }
        )
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        csv_rows = list(csv.DictReader(config.csv_path.open(encoding="utf-8", newline="")))
        assert len(csv_rows) == 1
        assert csv_rows[0]["geo_tags"] == ""


class TestExperienceEvidencePersistence:
    def test_unknown_experience_survives_a_round_trip(self, config) -> None:
        source = FakeSource(
            {
                "acme": [
                    raw_job(
                        "quiet",
                        "Software Engineer",
                        description="<p>You will build backend services in Go.</p>",
                    )
                ]
            }
        )
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        row = jobs_by_id(config)["quiet"]
        assert row["experience_category"] == "experience_unknown"
        assert row["experience_summary"] == "Not stated"
        assert row["blocker_status"] != "blocked"

        csv_rows = list(csv.DictReader(config.csv_path.open(encoding="utf-8", newline="")))
        exported = next(row for row in csv_rows if row["title"] == "Software Engineer")
        assert exported["experience_category"] == "experience_unknown"

    def test_a_blocked_job_keeps_its_entry_level_score(self, config) -> None:
        """Fit and eligibility are different questions; both stay visible."""

        source = FakeSource(
            {
                "acme": [
                    raw_job(
                        "blocked",
                        "Software Engineer, New Grad",
                        description=(
                            "<p>0-2 years of experience. This role requires U.S. "
                            "citizenship.</p>"
                        ),
                    )
                ]
            }
        )
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        row = jobs_by_id(config)["blocked"]
        assert row["match_category"] == "likely_ineligible"
        assert row["search_score"] <= 20
        assert row["entry_level_score"] >= 60
        assert row["blocker_status"] == "blocked"


class TestAdapterContract:
    def test_the_pipeline_runs_on_the_documented_adapter_surface_alone(self, config) -> None:
        """Keeping the surface small is what lets other ATS adapters slot in.

        ``FakeSource`` implements only ``fetch_company_jobs``, ``validate_company``
        and ``close`` — the same three methods a Greenhouse or Lever adapter would
        need to provide.
        """

        source = FakeSource({"acme": [raw_job("j1", "Backend Engineer")]})
        run_pipeline(config, source=source, discoverers=[FakeDiscoverer(["acme"])])
        assert source.fetched == ["acme"]
        assert {"fetch_company_jobs", "validate_company", "close"} <= set(vars(FakeSource))
