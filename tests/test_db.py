"""Persistence tests.

The behavior that matters most here is negative: a posting must never be closed
because a fetch failed, and history must never be rewritten by a later run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobfinder.db import Database
from jobfinder.models import CompanyCandidate, NormalizedJob


def job(job_id: str, title: str = "Software Engineer, New Grad", **overrides) -> NormalizedJob:
    values = {
        "source": "ashby",
        "source_company_slug": "acme",
        "source_job_id": job_id,
        "company_name": "Acme",
        "title": title,
        "location": "New York, NY",
        "description_text": "We welcome recent graduates.",
        "job_url": f"https://jobs.ashbyhq.com/acme/{job_id}",
        "apply_url": f"https://jobs.ashbyhq.com/acme/{job_id}/application",
        "is_software": True,
        "search_score": 80,
        "match_category": "strong_match",
    }
    values.update(overrides)
    return NormalizedJob(**values)


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    store = Database(tmp_path / "jobs.sqlite3")
    store.initialize()
    yield store
    store.close()


def by_source_id(database: Database) -> dict[str, dict]:
    return {row["source_job_id"]: row for row in database.list_jobs(active=None)}


class TestIngestion:
    def test_first_ingestion_inserts_and_marks_jobs_new(self, database: Database) -> None:
        run = database.start_run()
        stats = database.ingest_company_jobs("acme", [job("a"), job("b")], run)

        assert (stats.inserted, stats.closed) == (2, 0)
        rows = by_source_id(database)
        assert set(rows) == {"a", "b"}
        assert all(row["active"] and row["new_since_previous_run"] for row in rows.values())

    def test_second_run_does_not_duplicate_or_rewrite_history(self, database: Database) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a"), job("b")], first)
        original = by_source_id(database)

        second = database.start_run()
        stats = database.ingest_company_jobs("acme", [job("a"), job("b")], second)

        assert (stats.inserted, stats.unchanged, stats.closed) == (0, 2, 0)
        rows = by_source_id(database)
        assert len(rows) == 2
        for key, row in rows.items():
            assert row["first_seen_at"] == original[key]["first_seen_at"]
            assert row["last_seen_at"] >= original[key]["last_seen_at"]
            assert not row["new_since_previous_run"], "a second sighting is no longer new"

    def test_new_job_in_a_later_run_is_the_only_one_marked_new(self, database: Database) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], first)
        second = database.start_run()
        database.ingest_company_jobs("acme", [job("a"), job("c")], second)

        rows = by_source_id(database)
        assert rows["c"]["new_since_previous_run"] is True
        assert rows["a"]["new_since_previous_run"] is False
        assert rows["c"]["discovered_run_id"] == second

    def test_changed_content_updates_and_records_an_observation(self, database: Database) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], first)
        second = database.start_run()
        stats = database.ingest_company_jobs(
            "acme", [job("a", title="Software Engineer I")], second
        )

        assert (stats.updated, stats.inserted) == (1, 0)
        rows = by_source_id(database)
        assert rows["a"]["title"] == "Software Engineer I"
        events = {item["event_type"] for item in database.list_observations(run_id=second)}
        assert "updated" in events

    def test_identity_is_the_source_id_not_the_title(self, database: Database) -> None:
        run = database.start_run()
        database.ingest_company_jobs(
            "acme", [job("a", title="Software Engineer"), job("b", title="Software Engineer")], run
        )
        assert len(by_source_id(database)) == 2

    def test_a_duplicate_id_in_one_response_is_rejected_atomically(self, database: Database) -> None:
        run = database.start_run()
        with pytest.raises(ValueError, match="Duplicate job id"):
            database.ingest_company_jobs("acme", [job("a"), job("a")], run)
        assert database.list_jobs(active=None) == []

    def test_a_malformed_job_leaves_existing_jobs_active(self, database: Database) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], first)

        second = database.start_run()
        with pytest.raises(ValueError):
            database.ingest_company_jobs("acme", [job("a"), job("b", title="")], second)

        rows = by_source_id(database)
        assert rows["a"]["active"] is True
        assert "b" not in rows


class TestDisappearedJobs:
    def test_missing_jobs_close_only_after_a_successful_board_fetch(
        self, database: Database
    ) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a"), job("b")], first)

        second = database.start_run()
        stats = database.ingest_company_jobs("acme", [job("a")], second)

        assert stats.closed == 1
        rows = by_source_id(database)
        assert rows["a"]["active"] is True
        assert rows["b"]["active"] is False
        assert rows["b"]["closed_at"]
        assert not rows["b"]["new_since_previous_run"]

    def test_a_failed_fetch_never_closes_jobs(self, database: Database) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a"), job("b")], first)

        database.record_company_failure("acme", "HTTP 503", inactive_after=3)
        database.record_company_failure("acme", "timeout", inactive_after=3)

        rows = by_source_id(database)
        assert all(row["active"] for row in rows.values()), "a failed fetch must not close jobs"
        company = database.get_company("acme")
        assert company["consecutive_failures"] == 2
        assert company["active"] is True

    def test_repeated_failures_deactivate_the_company_but_not_its_jobs(
        self, database: Database
    ) -> None:
        run = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], run)
        for _ in range(3):
            database.record_company_failure("acme", "HTTP 500", inactive_after=3)

        company = database.get_company("acme")
        assert company["active"] is False
        assert company["consecutive_failures"] == 3
        assert by_source_id(database)["a"]["active"] is True

    def test_closed_history_is_preserved_not_deleted(self, database: Database) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a"), job("b")], first)
        second = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], second)

        assert len(database.list_jobs(active=None)) == 2
        assert len(database.list_jobs(active=True)) == 1
        closed = [item for item in database.list_observations(run_id=second) if item["event_type"] == "closed"]
        assert len(closed) == 1

    def test_a_returning_job_is_reopened_with_its_original_first_seen(
        self, database: Database
    ) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a"), job("b")], first)
        original = by_source_id(database)["b"]["first_seen_at"]

        second = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], second)
        third = database.start_run()
        stats = database.ingest_company_jobs("acme", [job("a"), job("b")], third)

        assert stats.reopened == 1
        row = by_source_id(database)["b"]
        assert row["active"] is True
        assert row["closed_at"] is None
        assert row["reopened_count"] == 1
        assert row["first_seen_at"] == original


class TestObservationHistory:
    def test_meaningful_events_are_recorded(self, database: Database) -> None:
        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a"), job("b")], first)
        second = database.start_run()
        database.ingest_company_jobs("acme", [job("a", title="Software Engineer I")], second)

        events = {
            (item["job_id"], item["event_type"])
            for item in database.list_observations()
        }
        kinds = {event for _, event in events}
        assert "new" in kinds and "updated" in kinds and "closed" in kinds

    def test_an_unchanged_sighting_writes_no_history_row(self, database: Database) -> None:
        """last_seen_at already records it; a row per job per run grows forever."""

        first = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], first)
        second = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], second)

        assert database.list_observations(run_id=second) == []
        assert len(database.list_observations()) == 1
        assert by_source_id(database)["a"]["last_seen_run_id"] == second

    def test_the_source_snapshot_does_not_duplicate_the_description(
        self, database: Database
    ) -> None:
        run = database.start_run()
        body = "<p>" + "x" * 5000 + "</p>"
        database.ingest_company_jobs(
            "acme",
            [
                job(
                    "a",
                    description_html=body,
                    raw_data={"id": "a", "descriptionHtml": body, "address": {"x": 1}},
                )
            ],
            run,
        )
        stored = by_source_id(database)["a"]

        assert stored["description_html"] == body, "the description itself is still kept"
        assert "descriptionHtml" not in stored["raw_data"]
        assert stored["raw_data"]["address"] == {"x": 1}, "other source fields survive"

    def test_snapshots_exclude_bulky_duplicated_payloads(self, database: Database) -> None:
        run = database.start_run()
        database.ingest_company_jobs(
            "acme",
            [job("a", description_html="<p>" + "x" * 5000 + "</p>")],
            run,
        )
        snapshot = database.list_observations()[0]["snapshot"]

        assert "description_html" not in snapshot
        assert "raw_data" not in snapshot
        assert snapshot["title"] and snapshot["source_job_id"] == "a"


class TestMigration:
    def test_a_version_one_database_is_compacted_in_place(self, tmp_path: Path) -> None:
        """Old databases stored a full job payload per job per run."""

        path = tmp_path / "old.sqlite3"
        store = Database(path)
        store.initialize()
        run = store.start_run()
        store.ingest_company_jobs("acme", [job("a"), job("b")], run)

        # Recreate the version 1 shape: bulky snapshots plus per-run 'seen' rows.
        connection = store.connect()
        fat = json.dumps(
            {
                "title": "Software Engineer, New Grad",
                "description_html": "<p>" + "x" * 20000 + "</p>",
                "raw_data": {"descriptionHtml": "y" * 20000},
            }
        )
        connection.execute("UPDATE job_observations SET snapshot_json = ?", (fat,))
        connection.execute(
            "UPDATE jobs SET source_raw_json = ?",
            (json.dumps({"id": "a", "descriptionHtml": "z" * 20000, "address": {"x": 1}}),),
        )
        for job_row in connection.execute("SELECT id FROM jobs").fetchall():
            connection.execute(
                "INSERT INTO job_observations(job_id, run_id, observed_at, event_type,"
                " content_hash, snapshot_json) VALUES (?, ?, '2026-01-01T00:00:00Z',"
                " 'seen', 'hash', ?)",
                (job_row["id"], run, fat),
            )
        connection.execute("PRAGMA user_version = 1")
        connection.close()
        store.close()

        jobs_before = len(Database(path).list_jobs(active=None))
        size_before = path.stat().st_size

        migrated = Database(path)
        migrated.initialize()
        try:
            observations = migrated.list_observations()
            assert len(migrated.list_jobs(active=None)) == jobs_before, "jobs must survive"
            assert all(item["event_type"] != "seen" for item in observations)
            assert observations, "meaningful history must be kept"
            for item in observations:
                assert "description_html" not in item["snapshot"]
                assert "raw_data" not in item["snapshot"]
            for row in migrated.list_jobs(active=None):
                assert "descriptionHtml" not in row["raw_data"]
                assert row["raw_data"]["address"] == {"x": 1}
        finally:
            migrated.close()

        assert path.stat().st_size < size_before

    def test_a_future_schema_version_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "future.sqlite3"
        store = Database(path)
        store.initialize()
        connection = store.connect()
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        store.close()

        with pytest.raises(RuntimeError, match="newer than supported"):
            Database(path).initialize()


class TestCompanyRegistry:
    def test_a_candidate_is_registered_unverified_with_provenance(
        self, database: Database
    ) -> None:
        run = database.start_run()
        database.register_company_candidate(
            CompanyCandidate(
                slug="acme",
                discovery_source="public_dataset",
                discovery_evidence="https://example.test/list.md",
            ),
            run_id=run,
        )
        company = database.get_company("acme")
        assert company["active"] is False, "a candidate is not a verified board"
        assert company["last_success_at"] is None
        assert company["discovery_source"] == "public_dataset"
        assert company["discovery_evidence"] == "https://example.test/list.md"

    def test_a_successful_fetch_verifies_and_clears_failures(self, database: Database) -> None:
        database.record_company_failure("acme", "HTTP 500", inactive_after=2)
        database.record_company_failure("acme", "HTTP 500", inactive_after=2)
        assert database.get_company("acme")["active"] is False

        run = database.start_run()
        database.ingest_company_jobs("acme", [job("a")], run, display_name="Acme")
        company = database.get_company("acme")
        assert company["active"] is True
        assert company["consecutive_failures"] == 0
        assert company["last_error"] is None
        assert company["last_job_count"] == 1
        assert company["display_name"] == "Acme"

    def test_slugs_and_urls_are_validated_before_storage(self, database: Database) -> None:
        assert database.upsert_company("https://jobs.ashbyhq.com/acme/some-job") > 0
        assert database.get_company("acme") is not None
        with pytest.raises(ValueError):
            database.upsert_company("https://example.com/acme")
        with pytest.raises(ValueError):
            database.upsert_company("../etc/passwd")


class TestSummaryCounts:
    def test_counts_reflect_only_persisted_rows(self, database: Database) -> None:
        run = database.start_run()
        database.ingest_company_jobs(
            "acme",
            [
                job("a"),
                job("b", title="Senior Software Engineer", search_score=5, match_category="likely_ineligible"),
                job("c", title="Account Executive", is_software=False, search_score=0, match_category="likely_ineligible"),
            ],
            run,
        )
        counts = database.summary_counts(run)
        assert counts["active_jobs"] == 3
        assert counts["software_jobs"] == 2
        assert counts["strong_matches"] == 1
        assert counts["new_strong_matches"] == 1
        assert counts["companies_known"] == 1
        assert counts["companies_verified"] == 1
