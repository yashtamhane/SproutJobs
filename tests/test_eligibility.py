"""Deterministic tests for the eligibility signal extractors."""

from __future__ import annotations

import pytest

from jobfinder.eligibility import (
    analyze_eligibility,
    classify_location,
    detect_graduation_window,
    detect_level_range,
    detect_seniority,
    detect_student_requirement,
    detect_title_cohort,
    detect_work_authorization,
    extract_year_requirements,
)


def _minima(text: str) -> list[tuple[int, int | None, str]]:
    return [(item.min_years, item.max_years, item.requirement) for item in extract_year_requirements(text)]


class TestYearsOfExperience:
    def test_range_is_captured_as_a_range(self) -> None:
        assert (0, 2, "required") in _minima("Requirements: 0-2 years of software experience.")

    def test_plus_form_is_a_minimum(self) -> None:
        assert (3, None, "required") in _minima("Requirements: 3+ years of engineering experience.")

    def test_preferred_wording_is_not_treated_as_required(self) -> None:
        found = extract_year_requirements("5 years of experience preferred, but not required.")
        assert found
        assert all(item.requirement == "preferred" for item in found)
        assert not any(item.required for item in found)

    def test_at_least_phrasing_is_required(self) -> None:
        assert (4, None, "required") in _minima("You must have at least 4 years of experience.")

    def test_benefits_durations_are_ignored(self) -> None:
        assert extract_year_requirements(
            "Equity vests over 4 years. Unlimited vacation after 1 year of employment."
        ) == ()

    def test_two_years_is_not_read_as_a_hard_blocker(self) -> None:
        requirements = extract_year_requirements("2+ years of professional experience required.")
        assert requirements[0].min_years == 2
        assert requirements[0].max_years is None


class TestThirdPartyExperience:
    """Durations describing the existing team are not requirements."""

    def test_team_tenure_prose_is_not_a_requirement(self) -> None:
        assert extract_year_requirements(
            "We hire at all levels: our team has new grads and product leaders with "
            "20 years experience."
        ) == ()

    def test_company_history_is_not_a_requirement(self) -> None:
        assert extract_year_requirements(
            "Founded in 2017, we have spent 8 years building the platform."
        ) == ()

    def test_a_real_requirement_beside_team_prose_still_counts(self) -> None:
        found = extract_year_requirements(
            "Our team has leaders with 20 years experience. "
            "Requirements: 3+ years of professional software engineering experience."
        )
        assert [(item.min_years, item.requirement) for item in found] == [(3, "required")]


class TestLevelRange:
    LADDER = (
        "We are hiring across multiple experience levels, including: Associate Software "
        "Engineer, Software Engineer, Senior Software Engineer, Staff Software Engineer."
    )

    def test_a_single_level_posting_is_not_multi_level(self) -> None:
        analysis = detect_level_range("Software Engineer", "Build services.")
        assert not analysis.multi_level
        assert analysis.status == "not_multi_level"

    def test_a_ladder_starting_at_associate_includes_entry(self) -> None:
        analysis = detect_level_range("Software Engineer - All Levels", self.LADDER)
        assert analysis.multi_level
        assert analysis.includes_entry is True
        assert analysis.status == "includes_entry"

    def test_required_years_override_the_advertised_ladder(self) -> None:
        analysis = detect_level_range(
            "Firmware Engineer - All Levels",
            self.LADDER + " What You'll Need: 3+ years of embedded firmware experience.",
        )
        assert analysis.includes_entry is False
        assert "3+ years" in analysis.reasons[0]

    def test_a_stated_mid_level_floor_excludes_entry(self) -> None:
        analysis = detect_level_range(
            "Software Engineer, Frontend (All Levels)",
            "We're looking for a Software Engineer, Frontend (Mid-level to Senior).",
        )
        assert analysis.includes_entry is False

    def test_naming_new_grads_includes_entry(self) -> None:
        analysis = detect_level_range(
            "Product Engineer (all levels)",
            "We hire at all levels: our team has new grads and product leaders.",
        )
        assert analysis.includes_entry is True

    def test_silence_about_seniority_stays_undetermined(self) -> None:
        analysis = detect_level_range(
            "Software Engineer, Core Platform (All Levels)",
            "You will build complex, scalable software systems.",
        )
        assert analysis.multi_level
        assert analysis.includes_entry is None
        assert analysis.status == "undetermined"

    @pytest.mark.parametrize(
        "description",
        [
            "Our base salary range reflects multiple levels. We assess level on performance.",
            "Base salary ranges are determined by experience, and we hire at all levels for "
            "multiple roles.",
            "The compensation range covers all levels for this job family.",
        ],
    )
    def test_pay_band_boilerplate_is_not_a_multi_level_claim(self, description: str) -> None:
        """A salary table spanning levels says nothing about who may apply."""

        assert not detect_level_range("Software Engineer II", description).multi_level

    def test_the_aggregate_analysis_exposes_the_level_range(self) -> None:
        analysis = analyze_eligibility("Software Engineer - All Levels", self.LADDER)
        assert analysis.level_range.includes_entry is True


class TestSeniority:
    @pytest.mark.parametrize(
        ("title", "level"),
        [
            ("Senior Software Engineer", "senior"),
            ("Staff Software Engineer", "staff"),
            ("Principal Engineer", "principal"),
            ("Engineering Manager", "manager"),
            ("Software Engineer III", "senior"),
            ("Software Engineer I", "entry"),
            ("Junior Backend Developer", "junior"),
            ("Associate Software Engineer", "associate"),
        ],
    )
    def test_titles_map_to_levels(self, title: str, level: str) -> None:
        assert detect_seniority(title).level == level

    def test_plain_title_is_uncertain_without_experience_text(self) -> None:
        analysis = detect_seniority("Software Engineer")
        assert analysis.level == "unknown"
        assert analysis.uncertain

    def test_required_experience_drives_senior_detection(self) -> None:
        analysis = detect_seniority("Software Engineer", "Requires 6+ years of experience.")
        assert analysis.likely_senior
        assert "five_plus_years_required" in analysis.signals

    def test_low_requirement_reads_as_entry_level(self) -> None:
        analysis = detect_seniority("Software Engineer", "Requirements: 0-2 years of experience.")
        assert analysis.likely_entry_level
        assert not analysis.likely_senior


class TestWorkAuthorization:
    @pytest.mark.parametrize(
        ("text", "flag"),
        [
            ("This role is open to U.S. citizens only.", "citizenship_required"),
            ("Applicants must be U.S. citizens or permanent residents only.", "permanent_resident_only"),
            ("We do not offer visa sponsorship for this position.", "no_sponsorship"),
            (
                "Candidates must not now or in the future require sponsorship for employment.",
                "must_not_require_sponsorship",
            ),
            ("An active Top Secret clearance is required.", "security_clearance_required"),
            # ITAR notices enumerate the accepted statuses.
            (
                "ITAR Requirements. To conform to U.S. Government export regulations, "
                "applicant must be a (i) U.S. citizen or national, (ii) U.S. lawful, "
                "permanent resident (aka green card holder), (iii) Refugee.",
                "citizenship_required",
            ),
        ],
    )
    def test_explicit_restrictions_are_flagged(self, text: str, flag: str) -> None:
        analysis = detect_work_authorization(text)
        assert flag in analysis.flags
        assert analysis.status == "likely_ineligible"
        assert analysis.explicit_blocker

    def test_silence_is_not_treated_as_rejection(self) -> None:
        analysis = detect_work_authorization("We build great products with great people.")
        assert analysis.status == "unspecified"
        assert not analysis.explicit_blocker
        assert analysis.uncertain

    def test_offered_sponsorship_is_recorded_positively(self) -> None:
        analysis = detect_work_authorization("Visa sponsorship is available for this role.")
        assert analysis.status == "positive"
        assert "sponsorship_available" in analysis.flags

    def test_generic_authorization_language_stays_uncertain(self) -> None:
        analysis = detect_work_authorization(
            "You must be legally authorized to work in the United States."
        )
        assert analysis.status == "uncertain"
        assert not analysis.explicit_blocker

    def test_neutral_with_or_without_sponsorship_is_not_a_blocker(self) -> None:
        analysis = detect_work_authorization(
            "We consider applicants with or without sponsorship needs."
        )
        assert not analysis.explicit_blocker


class TestGraduationWindow:
    def test_matching_cohort_is_compatible(self) -> None:
        analysis = detect_graduation_window("Open to students graduating in 2026.")
        assert analysis.status == "compatible"
        assert analysis.compatible is True

    def test_later_cohort_only_is_incompatible(self) -> None:
        analysis = detect_graduation_window("You must be graduating in 2027.")
        assert analysis.status == "incompatible"
        assert analysis.compatible is False
        assert "future_graduation_cohort_only" in analysis.flags

    def test_spring_2026_season_is_recognized(self) -> None:
        analysis = detect_graduation_window("For candidates graduating Spring 2026.")
        assert analysis.compatible is True

    def test_dated_window_containing_may_2026_is_compatible(self) -> None:
        analysis = detect_graduation_window(
            "Candidates must be graduating between December 2025 and June 2026."
        )
        assert analysis.compatible is True

    def test_silence_is_unspecified_not_incompatible(self) -> None:
        analysis = detect_graduation_window("Build backend services with us.")
        assert analysis.status == "unspecified"
        assert analysis.compatible is None
        assert analysis.uncertain


class TestTitleCohort:
    """Many postings state the cohort only in the title."""

    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer (2027 University Grad)",
            "Software Engineer, New Grad 2027",
            "Software Engineer - Class of 2027",
        ],
    )
    def test_a_later_cohort_in_the_title_is_incompatible(self, title: str) -> None:
        analysis = detect_title_cohort(title)
        assert analysis is not None
        assert analysis.compatible is False
        assert "future_graduation_cohort_only" in analysis.flags

    def test_the_candidate_cohort_in_the_title_is_compatible(self) -> None:
        analysis = detect_title_cohort("Software Engineer, New Grad 2026")
        assert analysis is not None and analysis.compatible is True

    def test_a_title_without_a_year_says_nothing(self) -> None:
        assert detect_title_cohort("Software Engineer, New Grad") is None
        assert detect_title_cohort("Senior Software Engineer") is None

    def test_a_bare_year_elsewhere_in_the_title_is_ignored(self) -> None:
        assert detect_title_cohort("Software Engineer, Ads 2.0 Platform (Q3 2026)") is None

    def test_the_description_wins_when_it_is_explicit(self) -> None:
        analysis = analyze_eligibility(
            "Software Engineer (2027 University Grad)",
            "This role is open to candidates graduating in 2026.",
        )
        assert analysis.graduation.compatible is True

    def test_the_title_cohort_reaches_the_aggregate_analysis(self) -> None:
        analysis = analyze_eligibility(
            "Software Engineer (2027 University Grad)",
            "Build backend services in Python with a great team.",
        )
        assert analysis.graduation.compatible is False
        assert analysis.likely_ineligible


class TestStudentStatus:
    def test_return_to_school_requirement_is_incompatible(self) -> None:
        analysis = detect_student_requirement(
            "You must be returning to school after the internship concludes."
        )
        assert analysis.status == "incompatible"
        assert analysis.return_to_school_required

    def test_current_enrollment_needs_review(self) -> None:
        analysis = detect_student_requirement("Must be currently enrolled in a degree program.")
        assert analysis.status == "uncertain"
        assert analysis.currently_enrolled_required


class TestLocation:
    @pytest.mark.parametrize(
        ("location", "category"),
        [
            ("New York, NY", "nyc_nj"),
            ("Jersey City, NJ", "nyc_nj"),
            ("Austin, Texas", "other_us"),
            ("London, United Kingdom", "non_us"),
        ],
    )
    def test_locations_map_to_tiers(self, location: str, category: str) -> None:
        assert classify_location(location).category == category

    def test_remote_us_is_recognized(self) -> None:
        analysis = classify_location("Remote (US)", "Remote")
        assert analysis.category == "remote_us"
        assert analysis.remote
        assert analysis.us_eligible is True

    def test_structured_country_text_resolves_ambiguous_cities(self) -> None:
        analysis = classify_location(
            "Waterloo", secondary_locations=("Waterloo, Ontario, Canada",)
        )
        assert analysis.category == "non_us"
        assert analysis.us_eligible is False

    def test_bare_remote_stays_uncertain(self) -> None:
        analysis = classify_location("Remote")
        assert analysis.category == "remote_unspecified"
        assert analysis.us_eligible is None
        assert analysis.uncertain


class TestAggregateAnalysis:
    def test_blockers_are_aggregated_with_reasons(self) -> None:
        analysis = analyze_eligibility(
            "Senior Software Engineer",
            "We require 8+ years of experience. U.S. citizens only.",
            "New York, NY",
        )
        assert analysis.likely_ineligible
        assert "citizenship_required" in analysis.flags
        assert "senior_role" in analysis.flags
        assert analysis.reasons

    def test_clean_new_grad_posting_has_no_blockers(self) -> None:
        analysis = analyze_eligibility(
            "Software Engineer, New Grad",
            "We welcome recent graduates. 0-2 years of experience.",
            "New York, NY",
        )
        assert not analysis.likely_ineligible
        assert analysis.authorization_flag == "unspecified"
