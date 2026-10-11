"""Tests for display canonicalization.

The reference case is modelled on a real board that published one "Software
Engineer, Core Platform (All Levels)" opening under six different Ashby ids,
seconds apart, with byte-identical descriptions.  The fixture below is
synthetic.  A reader should see that once.

The opposite failure matters more, though.  Hiding a real opening costs a job
seeker an application; showing one twice costs a scroll.  So the tests below
spend most of their effort pinning down what must *stay* separate.
"""

from __future__ import annotations

import pytest

from jobfinder.canonical import (
    CanonicalOpportunity,
    canonical_key,
    canonical_rows,
    canonicalize,
    same_opportunity,
)


FABRIKAM_DESCRIPTION = (
    "Why Fabrikam: Fabrikam builds planning software for industrial manufacturers. We help "
    "engineering, purchasing and production teams work from one shared plan instead of a "
    "dozen spreadsheets, and we automate the handoffs between them so orders ship on time. "
    "You will design, build and maintain distributed backend services in Python and Go, "
    "own features end to end, and work directly with the founders on the core platform "
    "that everything else depends on."
)


def fabrikam(source_id: str, **overrides) -> dict:
    """One of six synthetic duplicate postings, identical apart from its id."""

    row = {
        "source": "ashby",
        "source_company_slug": "fabrikam",
        "source_job_id": source_id,
        "company_name": "Fabrikam",
        "title": "Software Engineer, Core Platform (All Levels)",
        "department": "Engineering",
        "team": "Engineering",
        "location": "HQ (San Francisco, CA)",
        "workplace_type": "OnSite",
        "employment_type": "FullTime",
        "description_text": FABRIKAM_DESCRIPTION,
        "published_at": "2026-08-20T19:40:50Z",
        "first_seen_at": "2026-08-20T21:18:04Z",
        "active": True,
        "is_software": True,
        "search_score": 45,
        "match_category": "possible_match",
        "job_url": f"https://jobs.ashbyhq.com/fabrikam/{source_id}",
        "apply_url": f"https://jobs.ashbyhq.com/fabrikam/{source_id}/application",
    }
    row.update(overrides)
    return row


FABRIKAM_IDS = (
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000003",
    "00000000-0000-4000-8000-000000000004",
    "00000000-0000-4000-8000-000000000005",
    "00000000-0000-4000-8000-000000000006",
)


class TestFabrikamRegression:
    """(a) Same company/title/location, different source ids -> one opportunity."""

    def test_six_identical_postings_become_one_opportunity(self) -> None:
        records = [fabrikam(source_id) for source_id in FABRIKAM_IDS]

        opportunities = canonicalize(records)

        assert len(opportunities) == 1
        assert opportunities[0].duplicate_count == 6
        assert "same_description" in opportunities[0].reasons

    def test_every_source_id_is_retained_on_the_opportunity(self) -> None:
        """Nothing is thrown away; the extras are recorded, not deleted."""

        opportunity = canonicalize([fabrikam(source_id) for source_id in FABRIKAM_IDS])[0]

        assert set(opportunity.source_ids) == set(FABRIKAM_IDS)
        assert len(opportunity.duplicate_source_ids) == 5
        chosen = opportunity.record["source_job_id"]
        assert chosen not in opportunity.duplicate_source_ids

    def test_the_canonical_record_keeps_a_working_apply_link(self) -> None:
        row = canonical_rows([fabrikam(source_id) for source_id in FABRIKAM_IDS])[0]

        assert row["apply_url"] == f"https://jobs.ashbyhq.com/fabrikam/{row['source_job_id']}/application"
        assert row["source_job_id"] in FABRIKAM_IDS
        assert row["duplicate_count"] == 6
        assert len(row["duplicate_job_urls"]) == 5


class TestSeparateOpportunitiesSurvive:
    def test_same_title_different_locations_stay_separate(self) -> None:
        """(b) Four cities are four real choices for an applicant."""

        records = [
            fabrikam("a", location="London"),
            fabrikam("b", location="Sao Paulo"),
            fabrikam("c", location="Shanghai"),
            fabrikam("d", location="Tallinn"),
        ]

        assert len(canonicalize(records)) == 4

    def test_materially_different_descriptions_stay_separate(self) -> None:
        """(c) Same title and location, but different teams within the company."""

        robotics = fabrikam(
            "a",
            description_text=(
                "About the team. Our Field Robotics group writes the control software for "
                "autonomous forklifts in partner warehouses, from path planning and obstacle "
                "avoidance to the fleet dashboard that shift supervisors watch. Engineers "
                "here tune sensor fusion, replay recorded lidar logs in simulation, and "
                "visit customer sites to watch new vehicles handle pallets safely."
            ),
        )
        billing = fabrikam(
            "b",
            description_text=(
                "About the team. Our Billing and Payments group owns invoicing, tax "
                "calculation and payment reconciliation for every customer account, and "
                "keeps the ledger consistent across currencies and refunds. Engineers here "
                "design idempotent payment workflows, audit monthly revenue reports with "
                "finance, and handle disputed charges raised by enterprise buyers."
            ),
        )

        # Both are long enough to be compared, so they must stay apart on content
        # rather than through the too-short-to-compare fallback.
        assert same_opportunity(robotics, billing) == (False, "different_description")
        assert len(canonicalize([robotics, billing])) == 2

    def test_different_seniority_stays_separate(self) -> None:
        senior = fabrikam("a", title="Senior Software Engineer, Core Platform")
        staff = fabrikam("b", title="Staff Software Engineer, Core Platform")
        entry = fabrikam("c", title="Software Engineer I, Core Platform")

        assert len(canonicalize([senior, staff, entry])) == 3

    def test_different_workplace_type_stays_separate(self) -> None:
        onsite = fabrikam("a", workplace_type="OnSite")
        remote = fabrikam("b", workplace_type="Remote")

        assert len(canonicalize([onsite, remote])) == 2

    def test_different_employment_type_stays_separate(self) -> None:
        full_time = fabrikam("a", employment_type="FullTime")
        internship = fabrikam("b", employment_type="Intern")

        assert len(canonicalize([full_time, internship])) == 2

    def test_different_compensation_stays_separate(self) -> None:
        """A different pay band is a different offer even if the text matches."""

        junior = fabrikam("a", compensation_text="$120K – $150K")
        senior = fabrikam("b", compensation_text="$200K – $260K")

        matched, reason = same_opportunity(junior, senior)
        assert not matched
        assert reason == "different_compensation"
        assert len(canonicalize([junior, senior])) == 2

    def test_different_companies_never_merge(self) -> None:
        records = [fabrikam("a"), fabrikam("b", source_company_slug="other", company_name="Other")]

        assert len(canonicalize(records)) == 2

    def test_a_missing_description_is_not_assumed_to_match(self) -> None:
        described = fabrikam("a")
        blank = fabrikam("b", description_text="")

        matched, reason = same_opportunity(described, blank)
        assert not matched
        assert reason == "one_description_missing"

    def test_short_blurbs_require_an_exact_match(self) -> None:
        left = fabrikam("a", description_text="Backend engineer wanted. Python and Go.")
        right = fabrikam("b", description_text="Backend engineer wanted. Python and Rust.")

        matched, reason = same_opportunity(left, right)
        assert not matched
        assert reason == "descriptions_too_short_to_compare"


class TestMatchingRules:
    def test_a_shared_apply_url_collapses_regardless_of_id(self) -> None:
        shared = "https://jobs.ashbyhq.com/fabrikam/shared/application"
        left = fabrikam("a", apply_url=shared, description_text="One description.")
        right = fabrikam("b", apply_url=shared, description_text="A different description.")

        matched, reason = same_opportunity(left, right)
        assert matched
        assert reason == "same_apply_url"

    def test_trailing_slashes_do_not_defeat_url_matching(self) -> None:
        left = fabrikam("a", job_url="https://jobs.ashbyhq.com/fabrikam/shared/")
        right = fabrikam("b", job_url="https://JOBS.ashbyhq.com/fabrikam/shared")

        assert same_opportunity(left, right)[0]

    def test_near_identical_descriptions_collapse(self) -> None:
        base = FABRIKAM_DESCRIPTION
        left = fabrikam("a", description_text=base)
        right = fabrikam("b", description_text=base + " Apply today.")

        matched, reason = same_opportunity(left, right)
        assert matched
        assert reason in {"same_description", "near_identical_description"}

    def test_records_with_no_description_at_all_collapse(self) -> None:
        left = fabrikam("a", description_text="")
        right = fabrikam("b", description_text="")

        matched, reason = same_opportunity(left, right)
        assert matched
        assert reason == "no_distinguishing_detail"

    def test_requisition_suffixes_do_not_split_a_repost(self) -> None:
        left = fabrikam("a", title="Software Engineer, Core Platform (All Levels)")
        right = fabrikam("b", title="Software Engineer, Core Platform (All Levels) req 4821")

        assert canonical_key(left) == canonical_key(right)
        assert len(canonicalize([left, right])) == 1


class TestCanonicalSelection:
    def test_an_active_record_beats_a_closed_one(self) -> None:
        closed = fabrikam("a", active=False)
        active = fabrikam("b", active=True)

        opportunity = canonicalize([closed, active])[0]
        assert opportunity.record["source_job_id"] == "b"

    def test_a_more_complete_record_wins(self) -> None:
        sparse = fabrikam("a", compensation_text=None, department=None, team=None)
        complete = fabrikam("b", compensation_text="$150K – $190K")

        # Same pay text on both sides would block matching, so compare via URLs.
        sparse["apply_url"] = complete["apply_url"] = "https://jobs.ashbyhq.com/fabrikam/x/application"
        opportunity = canonicalize([sparse, complete])[0]
        assert opportunity.record["source_job_id"] == "b"

    def test_the_newest_published_record_wins_among_equals(self) -> None:
        older = fabrikam("a", published_at="2026-08-20T19:40:50Z")
        newer = fabrikam("b", published_at="2026-08-20T19:41:01Z")

        opportunity = canonicalize([older, newer])[0]
        assert opportunity.record["source_job_id"] == "b"

    def test_selection_is_stable_regardless_of_input_order(self) -> None:
        """A deterministic winner keeps reports from churning between runs."""

        records = [fabrikam(source_id) for source_id in FABRIKAM_IDS]
        forward = canonicalize(records)[0].record["source_job_id"]
        backward = canonicalize(list(reversed(records)))[0].record["source_job_id"]
        shuffled = canonicalize([records[3], records[0], records[5], records[1], records[2], records[4]])
        assert forward == backward == shuffled[0].record["source_job_id"]

    def test_identical_records_fall_back_to_the_source_id_tie_breaker(self) -> None:
        first = fabrikam("aaaa")
        second = fabrikam("bbbb")

        assert canonicalize([second, first])[0].record["source_job_id"] == "aaaa"


class TestOrderingAndShape:
    def test_input_ranking_is_preserved(self) -> None:
        top = fabrikam("top", title="Software Engineer, New Grad", search_score=95)
        middle = [fabrikam(source_id) for source_id in FABRIKAM_IDS]
        bottom = fabrikam("bottom", title="Backend Engineer", search_score=10)

        rows = canonical_rows([top, *middle, bottom])
        assert [row["title"] for row in rows] == [
            "Software Engineer, New Grad",
            "Software Engineer, Core Platform (All Levels)",
            "Backend Engineer",
        ]

    def test_unique_records_are_annotated_with_a_count_of_one(self) -> None:
        row = canonical_rows([fabrikam("only")])[0]

        assert row["duplicate_count"] == 1
        assert row["duplicate_source_ids"] == []
        assert row["canonical_key"] == canonical_key(row)

    def test_an_empty_input_is_handled(self) -> None:
        assert canonicalize([]) == []
        assert canonical_rows([]) == []

    def test_the_opportunity_exposes_its_source_records(self) -> None:
        opportunity = canonicalize([fabrikam(i) for i in FABRIKAM_IDS])[0]

        assert isinstance(opportunity, CanonicalOpportunity)
        assert len(opportunity.records) == 6
        assert all(record["source_company_slug"] == "fabrikam" for record in opportunity.records)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Software Engineer, Data Platform (All Levels)"),
        ("location", "New York, NY"),
        ("workplace_type", "Remote"),
        ("employment_type", "Intern"),
        ("source_company_slug", "someone-else"),
    ],
)
def test_any_displayed_field_difference_changes_the_key(field: str, value: str) -> None:
    assert canonical_key(fabrikam("a")) != canonical_key(fabrikam("a", **{field: value}))
