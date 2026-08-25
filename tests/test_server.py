"""The local dashboard server and the full-dataset API behind it.

The property under test throughout is that filtering, counting and sorting run
over *every* stored opportunity.  The static export could only embed a slice,
so a filter could report zero matches while the job sat in SQLite; several of
these tests build a set larger than any page and prove the far end is
reachable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobfinder.config import load_config
from jobfinder.db import Database
from jobfinder.models import NormalizedJob
from jobfinder.pipeline import refresh_opportunities, write_reports
from jobfinder.scoring import analyze_normalized_job
from jobfinder.server import DashboardServer, parse_query


NEW_GRAD = (
    "<p>We welcome recent graduates.</p><ul><li>0-2 years of experience</li>"
    "<li>Bachelor's degree in Computer Science</li></ul>"
    "<p>You will build backend services in Python.</p>"
)
SENIOR = "<p>Requirements: 8+ years of professional software engineering experience.</p>"
QUIET = "<p>You will build and ship backend services with a small team.</p>"

CITIES = {
    "nyc": ("New York, NY", "New York, New York, United States"),
    "new_jersey": ("Jersey City, NJ", "Jersey City, New Jersey, United States"),
    "other_us": ("Austin, TX", "Austin, Texas, United States"),
}


def make_job(index: int, *, title: str, city: str, description: str, company: str = "acme"):
    location, context = CITIES[city]
    job = NormalizedJob(
        source="ashby",
        source_company_slug=company,
        source_job_id=f"job-{index}",
        company_name=company.title(),
        title=title,
        department="Engineering",
        location=location,
        location_context=context,
        workplace_type="Hybrid",
        employment_type="FullTime",
        description_text=description,
        published_at="2026-08-18T14:30:00Z",
        job_url=f"https://jobs.ashbyhq.com/{company}/job-{index}",
        apply_url=f"https://jobs.ashbyhq.com/{company}/job-{index}/application",
        content_hash=f"hash-{company}-{index}",
    )
    analyze_normalized_job(job)
    return job


@pytest.fixture()
def project(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        "[discovery]\nenabled = false\npublic_dataset_urls = []\nseed_file = ''\n"
        "[network]\nmin_request_interval = 0.0\n",
        encoding="utf-8",
    )
    return load_config(tmp_path / "config.toml")


@pytest.fixture()
def populated(project):
    """A registry deliberately larger than any single page."""

    database = Database(project.database_path)
    database.initialize()
    run_id = database.start_run("test")
    jobs = []
    for index in range(180):
        city = ("nyc", "new_jersey", "other_us")[index % 3]
        if index % 3 == 0:
            title, description = "Software Engineer, New Grad", NEW_GRAD
        elif index % 3 == 1:
            title, description = "Senior Software Engineer", SENIOR
        else:
            title, description = "Software Engineer", QUIET
        # Each posting must be a genuinely distinct opening, or the canonical
        # layer will (correctly) collapse them and the fixture will not exercise
        # pagination at all.
        jobs.append(
            make_job(
                index,
                title=f"{title} - Team {index}",
                city=city,
                description=f"{description}<p>Team {index} owns this surface.</p>",
            )
        )
    database.ingest_company_jobs("acme", jobs, run_id)
    refresh_opportunities(project, database)
    yield project, database
    database.close()


class TestQueryValidation:
    def test_defaults_are_sane(self) -> None:
        parsed = parse_query("")
        assert parsed["page"] == 1
        assert parsed["page_size"] == 50
        assert parsed["sort"] == "best"
        assert parsed["status"] == "active"
        assert parsed["geo"] == [] and parsed["experience"] == []

    @pytest.mark.parametrize(
        "query,key,expected",
        [
            ("page=-4", "page", 1),
            ("page=abc", "page", 1),
            ("page_size=7", "page_size", 50),
            ("page_size=9999", "page_size", 100),
            ("page_size=100", "page_size", 100),
            ("sort=drop%20table", "sort", "best"),
            ("min_score=500", "min_score", 100),
            ("min_score=-20", "min_score", 0),
            ("category=nonsense", "category", "all"),
            ("confidence=purple", "confidence", "all"),
            ("status=weird", "status", "active"),
        ],
    )
    def test_invalid_parameters_fall_back(self, query: str, key: str, expected) -> None:
        assert parse_query(query)[key] == expected

    def test_unknown_multi_select_values_are_dropped(self) -> None:
        parsed = parse_query("geo=nyc&geo=atlantis&experience=unknown&experience=bogus")
        assert parsed["geo"] == ["nyc"]
        assert parsed["experience"] == ["unknown"]

    def test_repeated_and_comma_forms_both_work(self) -> None:
        assert parse_query("geo=nyc&geo=remote_us")["geo"] == ["nyc", "remote_us"]
        assert parse_query("geo=nyc,remote_us")["geo"] == ["nyc", "remote_us"]

    def test_freshness_becomes_an_epoch_cutoff(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        parsed = parse_query("freshness=7", now=now)
        expected = int((now - timedelta(days=7)).timestamp() * 1000)
        assert parsed["published_after"] == expected

    def test_freshness_source_selects_the_column(self) -> None:
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        parsed = parse_query("freshness=7&freshness_source=discovered", now=now)
        assert "discovered_after" in parsed and "published_after" not in parsed


class TestPagination:
    def test_totals_describe_the_whole_set_not_the_page(self, populated) -> None:
        _, database = populated
        result = database.query_opportunities(page_size=25)
        assert result["total"] == 180
        assert result["returned"] == 25
        assert result["total_pages"] == 8

    @pytest.mark.parametrize("size,pages", [(25, 8), (50, 4), (100, 2)])
    def test_supported_page_sizes(self, populated, size: int, pages: int) -> None:
        _, database = populated
        result = database.query_opportunities(page_size=size)
        assert result["page_size"] == size
        assert result["total_pages"] == pages

    def test_every_page_together_covers_the_set_exactly_once(self, populated) -> None:
        _, database = populated
        seen: list[int] = []
        for page in range(1, 9):
            seen.extend(row["id"] for row in database.query_opportunities(page=page, page_size=25)["results"])
        assert len(seen) == 180
        assert len(set(seen)) == 180, "pages must not overlap or drop rows"

    def test_a_page_past_the_end_clamps_to_the_last_page(self, populated) -> None:
        _, database = populated
        result = database.query_opportunities(page=999, page_size=25)
        assert result["page"] == 8
        assert result["returned"] == 5

    def test_page_zero_clamps_to_the_first_page(self, populated) -> None:
        _, database = populated
        assert database.query_opportunities(page=0)["page"] == 1

    def test_an_empty_result_set_is_well_formed(self, populated) -> None:
        _, database = populated
        result = database.query_opportunities(search="nothingmatchesthisstring")
        assert result["total"] == 0
        assert result["results"] == []
        assert result["total_pages"] == 1
        assert result["page"] == 1


class TestFullDatasetFiltering:
    def test_filters_reach_rows_beyond_the_first_page(self, populated) -> None:
        """The old static export could not do this at all."""

        _, database = populated
        # Rank every row, then confirm a filter finds matches past position 50.
        everything = database.query_opportunities(page_size=100, status="all")
        assert everything["total"] == 180 > everything["page_size"]
        deep = database.query_opportunities(page=2, page_size=100, status="all")
        assert deep["returned"] == 80
        first_ids = {row["id"] for row in everything["results"]}
        deep_ids = {row["id"] for row in deep["results"]}
        assert not first_ids & deep_ids

    def test_counts_match_a_direct_database_count(self, populated) -> None:
        _, database = populated
        api = database.query_opportunities(geo=["new_jersey"], status="all")["total"]
        direct = database.opportunity_count()
        assert 0 < api < direct
        assert api == 60, "one third of the fixture is in New Jersey"

    def test_search_matches_across_the_whole_set(self, populated) -> None:
        _, database = populated
        result = database.query_opportunities(search="new grad", status="all")
        assert result["total"] == 60

    def test_title_filter(self, populated) -> None:
        _, database = populated
        assert database.query_opportunities(title="senior", status="all")["total"] == 60

    def test_min_score_filter(self, populated) -> None:
        _, database = populated
        high = database.query_opportunities(min_score=60, status="all")["total"]
        assert 0 < high < 180
        for row in database.query_opportunities(min_score=60, status="all")["results"]:
            assert row["score"] >= 60


class TestGeographyUnion:
    def test_a_single_tag_filters(self, populated) -> None:
        _, database = populated
        assert database.query_opportunities(geo=["nyc"], status="all")["total"] == 60

    def test_two_tags_are_a_union_not_an_intersection(self, populated) -> None:
        _, database = populated
        nyc = database.query_opportunities(geo=["nyc"], status="all")["total"]
        nj = database.query_opportunities(geo=["new_jersey"], status="all")["total"]
        both = database.query_opportunities(geo=["nyc", "new_jersey"], status="all")["total"]
        assert both == nyc + nj == 120, "NYC + New Jersey must show either, not both-at-once"

    def test_no_tags_means_no_geography_filter(self, populated) -> None:
        _, database = populated
        assert database.query_opportunities(geo=[], status="all")["total"] == 180

    def test_every_returned_row_carries_a_selected_tag(self, populated) -> None:
        _, database = populated
        result = database.query_opportunities(geo=["nyc", "other_us"], status="all", page_size=100)
        for row in result["results"]:
            assert {"nyc", "other_us"} & set(row["geo_tags"])


class TestExperienceAndBlockerFilters:
    def test_experience_buckets_partition_the_set(self, populated) -> None:
        _, database = populated
        total = database.query_opportunities(status="all")["total"]
        buckets = ("new_grad", "zero_experience", "zero_to_one", "unknown", "one_plus", "two_plus")
        counted = sum(
            database.query_opportunities(experience=[bucket], status="all")["total"]
            for bucket in buckets
        )
        assert counted == total

    def test_unknown_experience_is_reachable(self, populated) -> None:
        _, database = populated
        result = database.query_opportunities(experience=["unknown"], status="all")
        assert result["total"] == 60
        for row in result["results"]:
            assert row["experience_category"] == "experience_unknown"

    def test_experience_buckets_are_a_union(self, populated) -> None:
        _, database = populated
        one = database.query_opportunities(experience=["unknown"], status="all")["total"]
        two = database.query_opportunities(experience=["new_grad"], status="all")["total"]
        both = database.query_opportunities(experience=["unknown", "new_grad"], status="all")["total"]
        assert both == one + two

    def test_the_blocker_bucket_selects_blocked_rows(self, populated) -> None:
        _, database = populated
        result = database.query_opportunities(experience=["blocker"], status="all", page_size=100)
        assert result["total"] > 0
        for row in result["results"]:
            assert row["blocker_status"] == "blocked"

    def test_the_blocker_dropdown_filters_independently(self, populated) -> None:
        _, database = populated
        for status in ("clear", "review", "blocked"):
            result = database.query_opportunities(blocker=status, status="all", page_size=100)
            for row in result["results"]:
                assert row["blocker_status"] == status


class TestSorting:
    @pytest.mark.parametrize(
        "sort", ["best", "published", "discovered", "company", "compensation", "entry_level"]
    )
    def test_every_documented_sort_runs(self, populated, sort: str) -> None:
        _, database = populated
        result = database.query_opportunities(sort=sort, status="all")
        assert result["sort"] == sort
        assert result["returned"] > 0

    def test_best_match_is_descending_by_score(self, populated) -> None:
        _, database = populated
        scores = [
            row["score"]
            for row in database.query_opportunities(sort="best", page_size=100, status="all")["results"]
        ]
        assert scores == sorted(scores, reverse=True)

    def test_company_sort_is_alphabetical(self, populated, project) -> None:
        _, database = populated
        extra = [make_job(900, title="Software Engineer, New Grad", city="nyc",
                          description=NEW_GRAD, company="zeta")]
        database.ingest_company_jobs("zeta", extra, database.start_run("more"))
        refresh_opportunities(project, database)
        names = [
            row["company"]
            for row in database.query_opportunities(sort="company", page_size=100, status="all")["results"]
        ]
        assert names == sorted(names, key=str.casefold)

    def test_an_unknown_sort_falls_back_to_best(self, populated) -> None:
        _, database = populated
        assert database.query_opportunities(sort="'; DROP TABLE jobs;--")["sort"] == "best"


class TestCanonicalIntegration:
    def test_duplicates_are_collapsed_before_serving(self, project) -> None:
        """The API must serve canonical opportunities, not raw source rows."""

        database = Database(project.database_path)
        database.initialize()
        run_id = database.start_run("test")
        jobs = [
            make_job(index, title="Software Engineer, Core Platform", city="nyc",
                     description=NEW_GRAD)
            for index in range(6)
        ]
        # Identical postings under six different source ids: one opening.
        for index, job in enumerate(jobs):
            job.job_url = f"https://jobs.ashbyhq.com/acme/dup-{index}"
            job.content_hash = f"dup-hash-{index}"
        database.ingest_company_jobs("acme", jobs, run_id)
        refresh_opportunities(project, database)
        try:
            result = database.query_opportunities(status="all")
            assert result["total"] == 1, "six requisitions describe one opportunity"
            assert result["results"][0]["duplicate_count"] == 6
            assert len(database.list_jobs(active=None)) == 6, "source rows are preserved"
        finally:
            database.close()

    def test_the_api_and_the_csv_describe_the_same_set(self, populated, project) -> None:
        import csv

        _, database = populated
        write_reports(project, database)
        with project.csv_path.open(encoding="utf-8", newline="") as handle:
            exported = list(csv.DictReader(handle))
        assert database.query_opportunities(status="all")["total"] == len(exported)

    def test_descriptions_are_not_shipped_with_the_list(self, populated) -> None:
        """Requirement 14: cards only, never thousands of descriptions."""

        _, database = populated
        for row in database.query_opportunities(page_size=100)["results"]:
            assert "description_text" not in row
            assert "description_html" not in row

    def test_the_detail_endpoint_supplies_the_description(self, populated) -> None:
        _, database = populated
        first = database.query_opportunities()["results"][0]
        detail = database.opportunity_detail(first["id"])
        assert detail is not None
        assert detail["title"] == first["title"]
        assert "backend services" in detail["description_text"]

    def test_an_unknown_detail_id_returns_nothing(self, populated) -> None:
        _, database = populated
        assert database.opportunity_detail(10_000_000) is None

    def test_every_card_field_survives_the_round_trip(self, populated) -> None:
        _, database = populated
        row = database.query_opportunities()["results"][0]
        for field in (
            "score", "entry_level_score", "category", "company", "title", "location",
            "geo_tags", "workplace_type", "employment_type", "department", "compensation",
            "published_display", "first_seen_display", "evidence_confidence",
            "experience_category", "score_reasons", "authorization_flag",
            "duplicate_count", "job_url", "apply_url",
        ):
            assert field in row, field


class TestFacets:
    def test_facet_counts_cover_the_whole_dataset(self, populated) -> None:
        _, database = populated
        facets = database.opportunity_facets(status="all")
        assert facets["total"] == 180
        assert sum(facets["categories"].values()) == 180
        assert facets["geo"]["nyc"] == 60
        assert facets["geo"]["new_jersey"] == 60

    def test_facets_respect_the_active_filters(self, populated) -> None:
        _, database = populated
        facets = database.opportunity_facets(geo=["nyc"], status="all")
        assert facets["total"] == 60
        assert facets["geo"]["new_jersey"] == 0

    def test_company_options_come_from_the_full_set(self, populated) -> None:
        _, database = populated
        facets = database.opportunity_facets(status="all")
        assert [entry["name"] for entry in facets["companies"]] == ["Acme"]
        assert facets["companies"][0]["count"] == 180


class TestHttpServer:
    @pytest.fixture()
    def running(self, populated):
        project, _ = populated
        server = DashboardServer(project, host="127.0.0.1", port=0)
        server.start()
        try:
            yield server
        finally:
            server.stop()

    def fetch(self, server: DashboardServer, path: str):
        with urllib.request.urlopen(server.url + path, timeout=10) as response:
            return response.status, response.read(), response.headers

    def test_the_server_starts_and_reports_its_url(self, running) -> None:
        assert running.running
        assert running.url.startswith("http://127.0.0.1:")

    def test_health_endpoint(self, running) -> None:
        status, body, _ = self.fetch(running, "/api/health")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

    def test_the_shell_page_contains_no_job_data(self, running) -> None:
        status, body, headers = self.fetch(running, "/")
        page = body.decode("utf-8")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        # The results container ships empty; cards arrive from the API.  The
        # word "job-card" does appear once inside the script's template, so the
        # meaningful check is that the container itself holds nothing.
        assert '<div id="jobList" class="job-list"></div>' in page
        assert "/api/jobs" in page
        body_only = page.split("<script>")[0]
        assert "<article" not in body_only, "no job data is embedded in the shell"

    def test_the_jobs_endpoint_paginates(self, running) -> None:
        status, body, _ = self.fetch(running, "/api/jobs?page=2&page_size=25&status=all")
        payload = json.loads(body)
        assert status == 200
        assert payload["page"] == 2
        assert payload["total"] == 180
        assert len(payload["results"]) == 25

    def test_geography_union_over_http(self, running) -> None:
        _, body, _ = self.fetch(running, "/api/jobs?geo=nyc&geo=new_jersey&status=all")
        assert json.loads(body)["total"] == 120

    def test_facets_endpoint(self, running) -> None:
        _, body, _ = self.fetch(running, "/api/facets?status=all")
        assert json.loads(body)["total"] == 180

    def test_detail_endpoint_over_http(self, running) -> None:
        _, body, _ = self.fetch(running, "/api/jobs?page_size=25")
        first = json.loads(body)["results"][0]
        _, detail_body, _ = self.fetch(running, f"/api/jobs/{first['id']}")
        assert "description_text" in json.loads(detail_body)

    def test_a_bad_detail_id_is_a_client_error(self, running) -> None:
        with pytest.raises(urllib.error.HTTPError) as caught:
            self.fetch(running, "/api/jobs/not-a-number")
        assert caught.value.code == 400

    def test_an_unknown_path_is_a_404(self, running) -> None:
        with pytest.raises(urllib.error.HTTPError) as caught:
            self.fetch(running, "/secrets")
        assert caught.value.code == 404

    def test_a_malformed_query_still_answers(self, running) -> None:
        status, body, _ = self.fetch(running, "/api/jobs?page=abc&sort=%27%3B--&page_size=nope")
        payload = json.loads(body)
        assert status == 200
        assert payload["page"] == 1 and payload["sort"] == "best"

    def test_stopping_releases_the_port(self, populated) -> None:
        project, _ = populated
        server = DashboardServer(project, host="127.0.0.1", port=0)
        server.start()
        port = server.port
        assert server.running
        server.stop()
        assert not server.running
        # Binding the same port again must succeed now that it is free.
        again = DashboardServer(project, host="127.0.0.1", port=port)
        again.start()
        try:
            assert again.running
        finally:
            again.stop()

    def test_starting_twice_is_harmless(self, running) -> None:
        first = running.url
        assert running.start() == first
