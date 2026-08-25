"""Tests for display canonicalization.

The reference case is real: the Heliux board published one "Software Engineer,
Core Platform (All Levels)" opening under six different Ashby ids, seconds apart,
with byte-identical descriptions.  A reader should see that once.

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


HELIUX_DESCRIPTION = (
    "Why Heliux: Heliux is building the AI-native operating system for the manufacturing "
    "enterprise. We are fundamentally reshaping how engineering and manufacturing companies "
    "run by eliminating cross-functional fragmentation and enabling agentic execution on a "
    "single shared operational reality. You will design, build and maintain distributed "
    "backend services in Python and Go, own features end to end, and work directly with "
    "founders on the core platform that everything else depends on."
)


def heliux(source_id: str, **overrides) -> dict:
    """One of the six real Heliux postings, identical apart from its id."""

    row = {
        "source": "ashby",
        "source_company_slug": "heliux",
        "source_job_id": source_id,
        "company_name": "Heliux",
        "title": "Software Engineer, Core Platform (All Levels)",
        "department": "Engineering",
        "team": "Engineering",
        "location": "HQ (San Francisco, CA)",
        "workplace_type": "OnSite",
        "employment_type": "FullTime",
        "description_text": HELIUX_DESCRIPTION,
        "published_at": "2026-08-20T19:40:50Z",
        "first_seen_at": "2026-08-20T21:18:04Z",
        "active": True,
        "is_software": True,
        "search_score": 45,
        "match_category": "possible_match",
        "job_url": f"https://jobs.ashbyhq.com/heliux/{source_id}",
        "apply_url": f"https://jobs.ashbyhq.com/heliux/{source_id}/application",
    }
    row.update(overrides)
    return row


HELIUX_IDS = (
    "8dc23bc1-616c-451c-a507-e0ed594911ab",
    "208394ff-89b0-4550-aab3-20fc6a1f526b",
    "dba7bcb8-32bd-4425-9049-c49c2034e39b",
    "fd02c124-0796-4b9a-bb89-b8c87137cef0",
    "bc497c47-c4da-4724-a040-4fc3a4874450",
    "fe78b13f-336c-4236-b786-cfd3ecd390f6",
)


class TestHeliuxRegression:
    """(a) Same company/title/location, different source ids -> one opportunity."""

    def test_six_identical_postings_become_one_opportunity(self) -> None:
        records = [heliux(source_id) for source_id in HELIUX_IDS]

        opportunities = canonicalize(records)

        assert len(opportunities) == 1
        assert opportunities[0].duplicate_count == 6
        assert "same_description" in opportunities[0].reasons

    def test_every_source_id_is_retained_on_the_opportunity(self) -> None:
        """Nothing is thrown away; the extras are recorded, not deleted."""

        opportunity = canonicalize([heliux(source_id) for source_id in HELIUX_IDS])[0]

        assert set(opportunity.source_ids) == set(HELIUX_IDS)
        assert len(opportunity.duplicate_source_ids) == 5
        chosen = opportunity.record["source_job_id"]
        assert chosen not in opportunity.duplicate_source_ids

    def test_the_canonical_record_keeps_a_working_apply_link(self) -> None:
        row = canonical_rows([heliux(source_id) for source_id in HELIUX_IDS])[0]

        assert row["apply_url"] == f"https://jobs.ashbyhq.com/heliux/{row['source_job_id']}/application"
        assert row["source_job_id"] in HELIUX_IDS
        assert row["duplicate_count"] == 6
        assert len(row["duplicate_job_urls"]) == 5


class TestSeparateOpportunitiesSurvive:
    def test_same_title_different_locations_stay_separate(self) -> None:
        """(b) Four cities are four real choices for an applicant."""

        records = [
            heliux("a", location="London"),
            heliux("b", location="Sao Paulo"),
            heliux("c", location="Shanghai"),
            heliux("d", location="Tallinn"),
        ]

        assert len(canonicalize(records)) == 4

    def test_materially_different_descriptions_stay_separate(self) -> None:
        """(c) Same title and location, but different teams within the company."""

        life_sciences = heliux(
            "a",
            description_text=(
                "About the team. Our Life Sciences team partners with major pharma firms, "
                "academic research centres and MedTech start-ups to design and deliver "
                "machine learning solutions for drug discovery and clinical trial design."
            ),
        )
        energy = heliux(
            "b",
            description_text=(
                "About the team. Our Energy Transition and Environment business unit is "
                "pioneering meaningful change in the clean energy revolution, deploying "
                "forecasting systems across national grid infrastructure and storage."
            ),
        )

        assert len(canonicalize([life_sciences, energy])) == 2

    def test_different_seniority_stays_separate(self) -> None:
        senior = heliux("a", title="Senior Software Engineer, Core Platform")
        staff = heliux("b", title="Staff Software Engineer, Core Platform")
        entry = heliux("c", title="Software Engineer I, Core Platform")

        assert len(canonicalize([senior, staff, entry])) == 3

    def test_different_workplace_type_stays_separate(self) -> None:
        onsite = heliux("a", workplace_type="OnSite")
        remote = heliux("b", workplace_type="Remote")

        assert len(canonicalize([onsite, remote])) == 2

    def test_different_employment_type_stays_separate(self) -> None:
        full_time = heliux("a", employment_type="FullTime")
        internship = heliux("b", employment_type="Intern")

        assert len(canonicalize([full_time, internship])) == 2

    def test_different_compensation_stays_separate(self) -> None:
        """A different pay band is a different offer even if the text matches."""

        junior = heliux("a", compensation_text="$120K – $150K")
        senior = heliux("b", compensation_text="$200K – $260K")

        matched, reason = same_opportunity(junior, senior)
        assert not matched
        assert reason == "different_compensation"
        assert len(canonicalize([junior, senior])) == 2

    def test_different_companies_never_merge(self) -> None:
        records = [heliux("a"), heliux("b", source_company_slug="other", company_name="Other")]

        assert len(canonicalize(records)) == 2

    def test_a_missing_description_is_not_assumed_to_match(self) -> None:
        described = heliux("a")
        blank = heliux("b", description_text="")

        matched, reason = same_opportunity(described, blank)
        assert not matched
        assert reason == "one_description_missing"

    def test_short_blurbs_require_an_exact_match(self) -> None:
        left = heliux("a", description_text="Backend engineer wanted. Python and Go.")
        right = heliux("b", description_text="Backend engineer wanted. Python and Rust.")

        matched, reason = same_opportunity(left, right)
        assert not matched
        assert reason == "descriptions_too_short_to_compare"


class TestMatchingRules:
    def test_a_shared_apply_url_collapses_regardless_of_id(self) -> None:
        shared = "https://jobs.ashbyhq.com/heliux/shared/application"
        left = heliux("a", apply_url=shared, description_text="One description.")
        right = heliux("b", apply_url=shared, description_text="A different description.")

        matched, reason = same_opportunity(left, right)
        assert matched
        assert reason == "same_apply_url"

    def test_trailing_slashes_do_not_defeat_url_matching(self) -> None:
        left = heliux("a", job_url="https://jobs.ashbyhq.com/heliux/shared/")
        right = heliux("b", job_url="https://JOBS.ashbyhq.com/heliux/shared")

        assert same_opportunity(left, right)[0]

    def test_near_identical_descriptions_collapse(self) -> None:
        base = HELIUX_DESCRIPTION
        left = heliux("a", description_text=base)
        right = heliux("b", description_text=base + " Apply today.")

        matched, reason = same_opportunity(left, right)
        assert matched
        assert reason in {"same_description", "near_identical_description"}

    def test_records_with_no_description_at_all_collapse(self) -> None:
        left = heliux("a", description_text="")
        right = heliux("b", description_text="")

        matched, reason = same_opportunity(left, right)
        assert matched
        assert reason == "no_distinguishing_detail"

    def test_requisition_suffixes_do_not_split_a_repost(self) -> None:
        left = heliux("a", title="Software Engineer, Core Platform (All Levels)")
        right = heliux("b", title="Software Engineer, Core Platform (All Levels) req 4821")

        assert canonical_key(left) == canonical_key(right)
        assert len(canonicalize([left, right])) == 1


class TestCanonicalSelection:
    def test_an_active_record_beats_a_closed_one(self) -> None:
        closed = heliux("a", active=False)
        active = heliux("b", active=True)

        opportunity = canonicalize([closed, active])[0]
        assert opportunity.record["source_job_id"] == "b"

    def test_a_more_complete_record_wins(self) -> None:
        sparse = heliux("a", compensation_text=None, department=None, team=None)
        complete = heliux("b", compensation_text="$150K – $190K")

        # Same pay text on both sides would block matching, so compare via URLs.
        sparse["apply_url"] = complete["apply_url"] = "https://jobs.ashbyhq.com/heliux/x/application"
        opportunity = canonicalize([sparse, complete])[0]
        assert opportunity.record["source_job_id"] == "b"

    def test_the_newest_published_record_wins_among_equals(self) -> None:
        older = heliux("a", published_at="2026-08-20T19:40:50Z")
        newer = heliux("b", published_at="2026-08-20T19:41:01Z")

        opportunity = canonicalize([older, newer])[0]
        assert opportunity.record["source_job_id"] == "b"

    def test_selection_is_stable_regardless_of_input_order(self) -> None:
        """A deterministic winner keeps reports from churning between runs."""

        records = [heliux(source_id) for source_id in HELIUX_IDS]
        forward = canonicalize(records)[0].record["source_job_id"]
        backward = canonicalize(list(reversed(records)))[0].record["source_job_id"]
        shuffled = canonicalize([records[3], records[0], records[5], records[1], records[2], records[4]])
        assert forward == backward == shuffled[0].record["source_job_id"]

    def test_identical_records_fall_back_to_the_source_id_tie_breaker(self) -> None:
        first = heliux("aaaa")
        second = heliux("bbbb")

        assert canonicalize([second, first])[0].record["source_job_id"] == "aaaa"


class TestOrderingAndShape:
    def test_input_ranking_is_preserved(self) -> None:
        top = heliux("top", title="Software Engineer, New Grad", search_score=95)
        middle = [heliux(source_id) for source_id in HELIUX_IDS]
        bottom = heliux("bottom", title="Backend Engineer", search_score=10)

        rows = canonical_rows([top, *middle, bottom])
        assert [row["title"] for row in rows] == [
            "Software Engineer, New Grad",
            "Software Engineer, Core Platform (All Levels)",
            "Backend Engineer",
        ]

    def test_unique_records_are_annotated_with_a_count_of_one(self) -> None:
        row = canonical_rows([heliux("only")])[0]

        assert row["duplicate_count"] == 1
        assert row["duplicate_source_ids"] == []
        assert row["canonical_key"] == canonical_key(row)

    def test_an_empty_input_is_handled(self) -> None:
        assert canonicalize([]) == []
        assert canonical_rows([]) == []

    def test_the_opportunity_exposes_its_source_records(self) -> None:
        opportunity = canonicalize([heliux(i) for i in HELIUX_IDS])[0]

        assert isinstance(opportunity, CanonicalOpportunity)
        assert len(opportunity.records) == 6
        assert all(record["source_company_slug"] == "heliux" for record in opportunity.records)


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
    assert canonical_key(heliux("a")) != canonical_key(heliux("a", **{field: value}))
