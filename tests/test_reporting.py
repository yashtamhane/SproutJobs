from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jobfinder.reporting import CSV_COLUMNS, generate_dashboard, generate_reports, write_csv


def sample_jobs():
    """Rows intentionally use the persistence layer's database column names."""

    return [
        {
            "source": "ashby",
            "source_company_slug": "acme",
            "source_job_id": "job-1",
            "company_name": "Acme & Sons",
            "title": "Software Engineer, New Grad",
            "department": "Product Engineering",
            "location": "New York, NY",
            "secondary_locations_json": '["Jersey City, NJ"]',
            "workplace_type": "Hybrid",
            "employment_type": "Full-time",
            "published_at": "2026-08-18T14:30:00Z",
            "first_seen_at": "2026-08-19T12:00:00+00:00",
            "last_seen_at": "2026-08-19T13:00:00+00:00",
            "job_url": "https://jobs.ashbyhq.com/acme/job-1?ref=a&view=1",
            "apply_url": "https://jobs.ashbyhq.com/acme/job-1/application",
            "compensation_text": "$105,000 – $135,000 / year",
            "active": 1,
            "is_software": 1,
            "search_score": 92,
            "match_category": "strong_match",
            "eligibility_flags_json": "[]",
            "score_reasons_json": json.dumps(
                [
                    {"signal": "new_grad_title", "delta": 35},
                    {"signal": "0_2_years", "delta": 25},
                ]
            ),
            "authorization_flag": "none",
            "new_since_previous_run": 1,
        },
        {
            "source": "ashby",
            "source_company_slug": "oldco",
            "company_name": "OldCo",
            "title": "Senior Backend Engineer",
            "location": "Remote — US",
            "workplace_type": "Remote",
            "employment_type": "Full-time",
            "published_at": "2026-07-01",
            "first_seen_at": "2026-07-02",
            "last_seen_at": "2026-08-01",
            "job_url": "https://jobs.ashbyhq.com/oldco/job-2",
            "apply_url": "https://jobs.ashbyhq.com/oldco/job-2/application",
            "compensation_text": "$175k-$205k",
            "active": 0,
            "is_software": 1,
            "search_score": 5,
            "match_category": "likely_ineligible",
            "eligibility_flags_json": json.dumps(["5+ years required"]),
            "score_reasons_json": json.dumps(
                [{"signal": "senior_title", "delta": -70}]
            ),
            "authorization_flag": "explicit_no_sponsorship",
            "new_since_previous_run": 0,
        },
    ]


class ReportingTests(unittest.TestCase):
    def test_generate_reports_creates_complete_dashboard_and_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = generate_reports(
                (row for row in sample_jobs()),
                root / "site" / "index.html",
                root / "exports" / "jobs.csv",
                generated_at=datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(result.job_count, 2)
            self.assertEqual(result.active_jobs, 1)
            self.assertEqual(result.software_jobs, 1)
            self.assertEqual(result.strong_matches, 1)
            self.assertEqual(result.new_strong_matches, 1)
            self.assertTrue(result.dashboard_path.is_absolute())
            self.assertTrue(result.csv_path.is_absolute())

            page = result.dashboard_path.read_text(encoding="utf-8")
            self.assertIn("Acme &amp; Sons", page)
            self.assertIn("Software Engineer, New Grad", page)
            self.assertIn("Jersey City, NJ", page)
            self.assertIn("$105,000 – $135,000 / year", page)
            self.assertIn("New Grad Title", page)
            self.assertIn('class="badge new-badge">New</span>', page)
            self.assertIn('class="badge closed-badge">Closed</span>', page)
            self.assertIn('data-auth="restriction"', page)
            self.assertIn('class="button apply"', page)
            self.assertIn("Apply <span", page)
            self.assertIn("View Job", page)
            self.assertIn("Work-authorization notes are keyword-based", page)
            self.assertIn("Aug 19, 2026 · 16:00 UTC", page)

            with result.csv_path.open(encoding="utf-8", newline="") as csv_file:
                reader = csv.DictReader(csv_file)
                exported = list(reader)
            self.assertEqual(tuple(reader.fieldnames or ()), CSV_COLUMNS)
            self.assertEqual(len(exported), 2)
            self.assertEqual(exported[0]["company"], "Acme & Sons")
            self.assertEqual(exported[0]["score"], "92")
            self.assertEqual(exported[0]["active"], "true")
            self.assertEqual(exported[0]["secondary_locations"], "Jersey City, NJ")
            reasons = json.loads(exported[0]["score_reasons"])
            self.assertEqual(reasons[0]["signal"], "new_grad_title")
            self.assertEqual(reasons[0]["delta"], 35)

    def test_dashboard_contains_every_required_filter_and_sort(self):
        with tempfile.TemporaryDirectory() as directory:
            path = generate_dashboard(sample_jobs(), Path(directory) / "index.html")
            page = path.read_text(encoding="utf-8")

        required_controls = (
            "searchInput",
            "titleInput",
            "companyFilter",
            "categoryFilter",
            "statusFilter",
            "softwareFilter",
            "locationInput",
            "remoteFilter",
            "authFilter",
            "freshnessSource",
            "freshnessFilter",
            "scoreFilter",
            "sortBy",
            "newOnly",
        )
        for control in required_controls:
            self.assertIn(f'id="{control}"', page)
        for option in (
            "Today",
            "Last 24 hours",
            "Last 3 days",
            "Last 7 days",
            "Last 14 days",
            "Last 30 days",
            "Newest published",
            "Newest discovered",
            "Company A–Z",
            "Highest compensation",
            "Active + closed",
            "Explicit restriction",
        ):
            self.assertIn(option, page)
        self.assertIn('value="active" selected', page)
        self.assertIn('value="software" selected', page)
        self.assertIn('aria-live="polite"', page)
        self.assertIn('class="skip-link"', page)
        self.assertIn("@media (max-width: 560px)", page)
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link rel=", page)

    def test_untrusted_content_is_escaped_and_unsafe_links_are_not_emitted(self):
        hostile = {
            "company_name": '<img src=x onerror="alert(1)">',
            "title": "<script>window.pwned=true</script>",
            "location": 'NYC" onmouseover="alert(2)',
            "search_score": 80,
            "match_category": "strong_match",
            "score_reasons_json": json.dumps(
                [{"signal": "x", "label": "<b>unsafe reason</b>", "delta": 5}]
            ),
            "eligibility_flags_json": json.dumps(["<svg/onload=alert(3)>"]),
            "authorization_flag": "uncertain",
            "active": True,
            "is_software": True,
            "job_url": "javascript:alert(4)",
            "apply_url": "data:text/html,<script>alert(5)</script>",
        }
        with tempfile.TemporaryDirectory() as directory:
            page = generate_dashboard(
                [hostile], Path(directory) / "index.html", title="Jobs <unsafe>"
            ).read_text(encoding="utf-8")

        self.assertIn("Jobs &lt;unsafe&gt;", page)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", page)
        self.assertIn("&lt;script&gt;window.pwned=true&lt;/script&gt;", page)
        self.assertIn("&lt;b&gt;unsafe reason&lt;/b&gt;", page)
        self.assertNotIn('<img src=x onerror="alert(1)">', page)
        self.assertNotIn("javascript:alert(4)", page)
        self.assertNotIn("data:text/html", page)
        self.assertIn("Job link unavailable", page)
        self.assertIn("Apply link unavailable", page)

    def test_csv_neutralizes_spreadsheet_formulas_and_invalid_urls(self):
        hostile = {
            "company_name": "=HYPERLINK(\"https://bad.invalid\")",
            "title": "+cmd|' /C calc'!A0",
            "location": "@SUM(1+1)",
            "job_url": "file:///etc/passwd",
            "apply_url": "javascript:alert(1)",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = write_csv([hostile], Path(directory) / "jobs.csv")
            with path.open(encoding="utf-8", newline="") as csv_file:
                row = next(csv.DictReader(csv_file))

        self.assertTrue(row["company"].startswith("'="))
        self.assertTrue(row["title"].startswith("'+"))
        self.assertTrue(row["location"].startswith("'@"))
        self.assertEqual(row["job_url"], "")
        self.assertEqual(row["apply_url"], "")

    def test_authorization_status_codes_are_filtered_without_overclaiming(self):
        rows = [
            {
                "company_name": "Unsaid Co",
                "title": "Software Engineer",
                "authorization_flag": "unspecified",
            },
            {
                "company_name": "Sponsor Co",
                "title": "Software Engineer",
                "authorization_flag": "positive",
                "eligibility_flags": ["sponsorship_available"],
            },
            {
                "company_name": "Review Co",
                "title": "Software Engineer",
                "authorization_flag": "uncertain",
            },
            {
                "company_name": "Blocked Co",
                "title": "Software Engineer",
                "authorization_flag": "likely_ineligible",
                "eligibility_flags": ["must_not_require_sponsorship"],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            page = generate_dashboard(rows, Path(directory) / "index.html").read_text(
                encoding="utf-8"
            )

        self.assertEqual(page.count('data-auth="clear"'), 2)
        self.assertEqual(page.count('data-auth="uncertain"'), 1)
        self.assertEqual(page.count('data-auth="restriction"'), 1)
        # The labels name a state precisely rather than claiming silence: a
        # posting that discussed visas must never read as "not mentioned".
        self.assertIn("No sponsorship information found", page)
        self.assertNotIn("Sponsorship not mentioned", page)
        self.assertIn("Sponsorship appears available", page)
        self.assertNotIn("Sponsorship Available</li>", page)
        self.assertIn("Explicit sponsorship restriction", page)

    def test_sqlite_rows_and_dataclasses_are_supported(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE jobs (company_name TEXT, title TEXT, search_score INTEGER, "
            "match_category TEXT, active INTEGER, is_software INTEGER)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            ("Database Co", "Backend Engineer", 73, "possible_match", 1, 1),
        )
        sqlite_row = connection.execute("SELECT * FROM jobs").fetchone()

        @dataclass
        class Job:
            company_name: str
            title: str
            search_score: int
            match_category: str

        with tempfile.TemporaryDirectory() as directory:
            path = generate_dashboard(
                [sqlite_row, Job("Object Co", "Platform Engineer", 81, "strong_match")],
                Path(directory) / "index.html",
            )
            page = path.read_text(encoding="utf-8")
        connection.close()

        self.assertIn("Database Co", page)
        self.assertIn("Object Co", page)
        self.assertIn('data-score="73.0"', page)
        self.assertIn('data-score="81.0"', page)

    def test_scoring_result_aliases_preserve_evidence_and_all_warnings(self):
        row = {
            "company": "Alias Co",
            "title": "Software Engineer I",
            "score": 88,
            "category": "strong_match",
            "is_software_role": True,
            "score_reasons": [],
            "reasons": [
                {
                    "signal": "level_one_title",
                    "delta": 25,
                    "evidence": "Engineer I",
                }
            ],
            "warnings": ["Requirements need manual review."],
            "hard_blockers": ["graduation_window_incompatible"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = generate_reports(
                (item for item in [row]), root / "index.html", root / "jobs.csv"
            )
            page = result.dashboard_path.read_text(encoding="utf-8")
            with result.csv_path.open(encoding="utf-8", newline="") as csv_file:
                exported = next(csv.DictReader(csv_file))

        self.assertIn("Level One Title", page)
        self.assertIn('data-auth="clear"', page)
        self.assertIn("Requirements need manual review.", page)
        self.assertIn("Graduation Window Incompatible", page)
        reason = json.loads(exported["score_reasons"])[0]
        self.assertEqual(reason["evidence"], "Engineer I")

    def test_generic_uncertainty_is_a_note_not_a_warning(self):
        """Most postings say nothing about sponsorship; that must not look alarming."""

        row = {
            "company_name": "Quiet Co",
            "title": "Software Engineer, New Grad",
            "search_score": 90,
            "match_category": "strong_match",
            "authorization_flag": "unspecified",
            "uncertain": True,
            "active": True,
            "is_software": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = generate_reports([row], root / "index.html", root / "jobs.csv")
            page = result.dashboard_path.read_text(encoding="utf-8")
            with result.csv_path.open(encoding="utf-8", newline="") as csv_file:
                exported = next(csv.DictReader(csv_file))

        self.assertIn("Some eligibility details are uncertain", page)
        self.assertIn('class="auth-clear"', page)
        self.assertNotIn("Review before applying", page)
        self.assertEqual(exported["uncertain"], "true")

    def test_a_real_restriction_still_produces_a_warning_block(self):
        row = {
            "company_name": "Blocked Co",
            "title": "Software Engineer",
            "authorization_flag": "likely_ineligible",
            "eligibility_flags": ["no_sponsorship"],
            "uncertain": True,
            "active": True,
            "is_software": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            page = generate_dashboard(
                [row], Path(directory) / "index.html"
            ).read_text(encoding="utf-8")

        self.assertIn("Review before applying", page)
        self.assertIn("No Sponsorship", page)
        self.assertIn('data-auth="restriction"', page)

    def test_supplied_totals_are_shown_instead_of_page_counts(self):
        """A capped page must not imply the database holds only what it shows."""

        with tempfile.TemporaryDirectory() as directory:
            page = generate_dashboard(
                sample_jobs(),
                Path(directory) / "index.html",
                totals={
                    "jobs_tracked": 15231,
                    "active_jobs": 15231,
                    "software_jobs": 3274,
                    "strong_matches": 86,
                    "new_jobs": 12,
                    "reportable_jobs": 3274,
                },
            ).read_text(encoding="utf-8")

        self.assertIn("<strong>15,231</strong><span>Jobs tracked</span>", page)
        self.assertIn("<strong>3,274</strong><span>Active software roles</span>", page)
        self.assertIn("This page lists 2 of 3,274 matching jobs", page)

    def test_without_totals_the_page_describes_only_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            page = generate_dashboard(
                sample_jobs(), Path(directory) / "index.html"
            ).read_text(encoding="utf-8")

        self.assertIn("This page lists all 2 matching jobs", page)
        self.assertIn("<strong>2</strong><span>Jobs tracked</span>", page)

    def test_freshness_defaults_to_any_time(self):
        with tempfile.TemporaryDirectory() as directory:
            page = generate_dashboard(
                sample_jobs(), Path(directory) / "index.html"
            ).read_text(encoding="utf-8")

        self.assertIn('<option value="all" selected>Any time</option>', page)

    def test_a_configured_freshness_window_is_preselected(self):
        with tempfile.TemporaryDirectory() as directory:
            page = generate_dashboard(
                sample_jobs(),
                Path(directory) / "index.html",
                default_freshness_days=7,
            ).read_text(encoding="utf-8")

        self.assertIn('<option value="7" selected>Last 7 days</option>', page)
        self.assertNotIn('<option value="all" selected>', page)
        # The control is still there, so the narrowed view is visible and undoable.
        self.assertIn('id="freshnessFilter"', page)
        self.assertIn("Any time", page)

    def test_a_collapsed_opportunity_discloses_how_many_postings_it_covers(self):
        row = {
            "company_name": "Heliux",
            "title": "Software Engineer, Core Platform (All Levels)",
            "location": "HQ (San Francisco, CA)",
            "search_score": 45,
            "match_category": "possible_match",
            "active": True,
            "is_software": True,
            "duplicate_count": 6,
            "duplicate_source_ids": ["id-2", "id-3", "id-4", "id-5", "id-6"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = generate_reports([row], root / "index.html", root / "jobs.csv")
            page = result.dashboard_path.read_text(encoding="utf-8")
            with result.csv_path.open(encoding="utf-8", newline="") as csv_file:
                exported = next(csv.DictReader(csv_file))

        self.assertIn('class="badge duplicate-badge"', page)
        self.assertIn("6× posted", page)
        self.assertEqual(exported["duplicate_count"], "6")
        self.assertEqual(exported["duplicate_source_ids"], "id-2; id-3; id-4; id-5; id-6")

    def test_a_unique_opportunity_shows_no_duplicate_badge(self):
        row = {
            "company_name": "Solo Co",
            "title": "Software Engineer",
            "active": True,
            "is_software": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = generate_reports([row], root / "index.html", root / "jobs.csv")
            page = result.dashboard_path.read_text(encoding="utf-8")
            with result.csv_path.open(encoding="utf-8", newline="") as csv_file:
                exported = next(csv.DictReader(csv_file))

        self.assertNotIn('class="badge duplicate-badge"', page)
        self.assertNotIn("× posted", page)
        self.assertEqual(exported["duplicate_count"], "1")
        self.assertEqual(exported["duplicate_source_ids"], "")

    def test_empty_report_is_still_valid_and_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = generate_reports([], root / "index.html", root / "jobs.csv")
            page = result.dashboard_path.read_text(encoding="utf-8")
            with result.csv_path.open(encoding="utf-8", newline="") as csv_file:
                exported = list(csv.reader(csv_file))

        self.assertEqual(result.job_count, 0)
        self.assertIn("No jobs match these filters", page)
        self.assertIn("Reset filters", page)
        self.assertEqual(exported, [list(CSV_COLUMNS)])


def _render(rows):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        result = generate_reports(rows, root / "index.html", root / "jobs.csv")
        page = result.dashboard_path.read_text(encoding="utf-8")
        with result.csv_path.open(encoding="utf-8", newline="") as csv_file:
            exported = list(csv.DictReader(csv_file))
    return page, exported


class GeographyFilterTests(unittest.TestCase):
    """Geography is multi-select, and nothing outside NYC/NJ is hidden."""

    def test_every_geography_bucket_gets_a_checkbox(self):
        page, _ = _render(sample_jobs())
        for value in ("nyc", "new_jersey", "remote_us", "other_us"):
            self.assertIn(f'class="geo-check" value="{value}"', page)
        for label in ("NYC", "New Jersey", "Remote US", "Other US"):
            self.assertIn(label, page)

    def test_the_filter_is_multi_select_not_a_dropdown(self):
        page, _ = _render(sample_jobs())
        self.assertIn('type="checkbox" class="geo-check"', page)
        self.assertNotIn('<select id="geoFilter"', page)

    def test_no_geography_is_preselected_so_all_us_regions_show(self):
        page, _ = _render(sample_jobs())
        checked = page.count('class="geo-check" value="nyc" checked')
        self.assertEqual(checked, 0)

    def test_a_card_carries_all_of_its_geography_tags(self):
        row = dict(sample_jobs()[0], geo_tags=["nyc", "new_jersey", "other_us"])
        page, exported = _render([row])
        self.assertIn('data-geo="nyc new_jersey other_us"', page)
        self.assertEqual(exported[0]["geo_tags"], "nyc; new_jersey; other_us")

    def test_an_out_of_state_job_is_rendered_not_dropped(self):
        row = dict(
            sample_jobs()[0],
            location="Austin, TX",
            geo_tags=["other_us"],
            title="Software Engineer, New Grad",
        )
        page, exported = _render([row])
        self.assertIn("Austin, TX", page)
        self.assertEqual(len(exported), 1)
        self.assertIn("Other US", page)


class ExperienceEvidenceTests(unittest.TestCase):
    """The dashboard has to explain *why* a score is low."""

    def test_every_experience_bucket_gets_a_checkbox(self):
        page, _ = _render(sample_jobs())
        for value in (
            "new_grad",
            "zero_experience",
            "zero_to_one",
            "unknown",
            "one_plus",
            "two_plus",
            "blocker",
        ):
            self.assertIn(f'class="experience-check" value="{value}"', page)

    def test_an_unknown_posting_says_not_stated_not_rejected(self):
        row = dict(
            sample_jobs()[0],
            search_score=0,
            match_category="weak_match",
            evidence_confidence="low",
            experience_category="experience_unknown",
            experience_summary="Not stated",
            experience_detail="No explicit experience requirement was found in this posting.",
            score_reasons_json=json.dumps(
                [{"signal": "experience_unknown", "delta": 0, "confidence": "low"}]
            ),
        )
        page, exported = _render([row])
        self.assertIn("Experience requirement not stated", page)
        self.assertIn("No explicit experience requirement was found", page)
        self.assertNotIn("No entry-level evidence found", page)
        self.assertIn('data-experience="experience_unknown"', page)
        self.assertIn('data-experience-bucket="unknown"', page)
        self.assertEqual(exported[0]["experience_category"], "experience_unknown")
        self.assertEqual(exported[0]["experience_summary"], "Not stated")

    def test_a_negative_posting_states_the_requirement(self):
        row = dict(
            sample_jobs()[0],
            search_score=0,
            match_category="weak_match",
            evidence_confidence="high",
            experience_category="three_plus_required",
            experience_summary="3+ years required",
            experience_detail="Experience requirement exceeds the new-grad target.",
            score_reasons_json=json.dumps(
                [{"signal": "three_plus_required", "delta": -55, "confidence": "high"}]
            ),
        )
        page, exported = _render([row])
        self.assertIn("Why this scores low", page)
        self.assertIn("3+ years required", page)
        self.assertIn("Experience requirement exceeds the new-grad target.", page)
        self.assertIn("high confidence", page)
        self.assertEqual(exported[0]["experience_summary"], "3+ years required")
        self.assertEqual(exported[0]["evidence_confidence"], "high")

    def test_unknown_and_negative_look_different_on_the_page(self):
        unknown = dict(
            sample_jobs()[0],
            source_job_id="unknown-1",
            experience_category="experience_unknown",
            experience_summary="Not stated",
        )
        negative = dict(
            sample_jobs()[0],
            source_job_id="negative-1",
            experience_category="three_plus_required",
            experience_summary="3+ years required",
        )
        page, _ = _render([unknown, negative])
        self.assertIn("experience-unknown", page)
        self.assertIn("experience-known", page)

    def test_an_unknown_row_is_preserved_in_both_outputs(self):
        """Potentially valid roles must not be hidden just for being quiet."""

        row = dict(
            sample_jobs()[0],
            experience_category="experience_unknown",
            experience_summary="Not stated",
            search_score=0,
            match_category="weak_match",
        )
        page, exported = _render([row])
        self.assertEqual(len(exported), 1)
        self.assertIn("Software Engineer, New Grad", page)
        # Rendered visible, not hidden: it is an active software role.
        self.assertNotIn('id="job-0" hidden', page)

    def test_entry_level_score_is_exported_separately_from_the_ranking_score(self):
        row = dict(
            sample_jobs()[0],
            search_score=20,
            entry_level_score=70,
            match_category="likely_ineligible",
        )
        _, exported = _render([row])
        self.assertEqual(exported[0]["score"], "20")
        self.assertEqual(exported[0]["entry_level_score"], "70")

    def test_blocker_status_is_exported(self):
        row = dict(sample_jobs()[0], blocker_status="blocked")
        page, exported = _render([row])
        self.assertEqual(exported[0]["blocker_status"], "blocked")
        self.assertIn('data-blocker="blocked"', page)


if __name__ == "__main__":
    unittest.main()


class EvidenceHeadingTests(unittest.TestCase):
    """A weak positive signal must not be dressed up as a reason it fits."""

    def _row(self, **overrides):
        base = dict(
            sample_jobs()[0],
            search_score=0,
            match_category="weak_match",
            evidence_confidence="low",
            experience_category="experience_unknown",
            experience_summary="Not stated",
            experience_detail="No explicit experience requirement was found in this posting.",
            score_reasons_json=json.dumps(
                [
                    {"signal": "experience_unknown", "delta": 0, "confidence": "low"},
                    {"signal": "bachelors_cs", "delta": 5, "confidence": "low"},
                ]
            ),
        )
        base.update(overrides)
        return base

    def test_a_low_confidence_bonus_does_not_claim_the_role_suits(self):
        page, _ = _render([self._row()])
        self.assertNotIn("Why this suits an entry-level candidate", page)
        self.assertIn("Experience requirement not stated", page)

    def test_substantive_evidence_does_earn_the_confident_heading(self):
        page, _ = _render(
            [
                self._row(
                    experience_category="explicit_new_grad",
                    experience_summary="New grad role",
                    evidence_confidence="high",
                    search_score=70,
                    match_category="strong_match",
                    score_reasons_json=json.dumps(
                        [{"signal": "explicit_new_grad", "delta": 70, "confidence": "high"}]
                    ),
                )
            ]
        )
        self.assertIn("Why this suits an entry-level candidate", page)

    def test_a_known_negative_says_why_the_score_is_low(self):
        page, _ = _render(
            [
                self._row(
                    experience_category="two_plus_years",
                    experience_summary="2+ years required",
                    experience_detail="Generally outside the new-grad target.",
                    evidence_confidence="medium",
                    score_reasons_json=json.dumps(
                        [
                            {"signal": "two_plus_years", "delta": -10, "confidence": "medium"},
                            {"signal": "bachelors_cs", "delta": 5, "confidence": "low"},
                        ]
                    ),
                )
            ]
        )
        self.assertIn("Why this scores low", page)
        self.assertIn("Generally outside the new-grad target.", page)
