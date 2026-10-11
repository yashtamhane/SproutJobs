"""Tests for software-role classification and entry-level match scoring."""

from __future__ import annotations

import pytest

from jobfinder.classify import classify_software_role
from jobfinder.models import NormalizedJob
from jobfinder.scoring import analyze_normalized_job, categorize_score, score_job


ENTRY_DESCRIPTION = (
    "We welcome recent graduates. Requirements: 0-2 years of experience, "
    "a Bachelor's degree in Computer Science, and internship experience is accepted. "
    "You will build and maintain backend services in Python."
)


def signals(result) -> set[str]:
    return {reason.signal for reason in result.reasons}


class TestSoftwareClassification:
    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer",
            "Software Developer",
            "Backend Engineer",
            "Full Stack Engineer",
            "Platform Engineer",
            "Infrastructure Engineer",
            "Product Engineer",
            "Application Engineer",
            "SWE I",
            "Machine Learning Engineer",
        ],
    )
    def test_target_titles_are_software_roles(self, title: str) -> None:
        assert classify_software_role(title, ENTRY_DESCRIPTION).is_software_role

    @pytest.mark.parametrize(
        "title",
        [
            "Product Manager",
            "Mechanical Engineer",
            "Data Analyst",
            "IT Help Desk Technician",
            "Recruiter, Technical",
            "Customer Success Manager",
            "Engineering Manager",
        ],
    )
    def test_non_software_titles_are_rejected(self, title: str) -> None:
        assert not classify_software_role(title, ENTRY_DESCRIPTION).is_software_role

    def test_sales_engineer_with_sales_duties_is_rejected(self) -> None:
        result = classify_software_role(
            "Solutions Engineer",
            "Partner with account executives through the sales cycle to close deals and run demos.",
        )
        assert not result.is_software_role

    def test_seniority_does_not_change_whether_it_is_software(self) -> None:
        assert classify_software_role("Senior Software Engineer", ENTRY_DESCRIPTION).is_software_role

    def test_description_keywords_alone_do_not_create_a_software_role(self) -> None:
        """Finance and data postings routinely mention Python, SQL, and systems."""

        result = classify_software_role(
            "Credit Risk Associate",
            "You will build and maintain risk systems, working in our codebase with Python "
            "and SQL against the data platform, applying algorithms to underwriting.",
            department="Risk",
        )
        assert not result.is_software_role
        assert "title_evidence_missing" in {reason.signal for reason in result.reasons}, (
            "the ceiling that kept this out of the software set should be auditable"
        )

    def test_engineering_department_supports_an_unusual_title(self) -> None:
        result = classify_software_role(
            "Member of Technical Staff",
            "Design, build and maintain distributed systems and REST APIs in Python and Go. "
            "You will work in our codebase daily on algorithms and microservices.",
            department="Engineering",
        )
        assert result.is_software_role
        assert result.uncertain, "an unusual title should not claim high confidence"


class TestScoring:
    def test_new_grad_posting_is_a_strong_match(self) -> None:
        result = score_job("Software Engineer, New Grad", ENTRY_DESCRIPTION, "New York, NY")
        assert result.category == "strong_match"
        assert result.score >= 65
        assert "explicit_new_grad" in signals(result)
        assert result.experience_category == "explicit_new_grad"
        assert "bachelors_cs" in signals(result)

    def test_generic_software_engineer_without_evidence_is_weak(self) -> None:
        """Being a software job is not evidence that a new graduate qualifies."""

        result = score_job(
            "Software Engineer",
            "Join our team to build products. We value curiosity.",
            "New York, NY",
        )
        assert result.category == "weak_match"
        assert result.evidence_confidence == "low"
        # Silence is recorded as *unknown*, never as "no evidence it fits".
        assert "experience_unknown" in signals(result)
        assert result.experience_unknown
        assert not result.positive_reasons or all(
            reason.confidence == "low" for reason in result.positive_reasons
        )

    def test_software_classification_contributes_nothing_to_the_score(self) -> None:
        result = score_job("Software Engineer", "Build services in Go.", None)
        assert result.is_software_role
        assert result.score == 0
        assert "software_role" not in signals(result)

    @pytest.mark.parametrize(
        "title",
        ["Senior Software Engineer", "Staff Software Engineer", "Principal Engineer", "Engineering Manager"],
    )
    def test_senior_titles_are_ineligible(self, title: str) -> None:
        result = score_job(title, ENTRY_DESCRIPTION, "New York, NY")
        assert result.category == "likely_ineligible"
        assert result.hard_blockers

    def test_five_plus_years_required_is_ineligible(self) -> None:
        result = score_job(
            "Software Engineer",
            "Requirements: 5+ years of professional software engineering experience.",
            "New York, NY",
        )
        assert result.category == "likely_ineligible"
        assert "hard_blocker" in signals(result)
        assert result.experience_category == "hard_blocker"

    def test_three_plus_years_required_is_penalized_but_not_blocked(self) -> None:
        result = score_job(
            "Software Engineer",
            "Requirements: 3+ years of professional experience building web services.",
            "New York, NY",
        )
        assert "three_plus_required" in signals(result)
        assert result.category != "likely_ineligible"

    def test_three_years_preferred_remains_eligible(self) -> None:
        result = score_job(
            "Software Engineer",
            "3 years of experience preferred. We welcome recent graduates.",
            "New York, NY",
        )
        assert result.category != "likely_ineligible"

    def test_citizenship_requirement_is_a_hard_blocker(self) -> None:
        result = score_job(
            "Software Engineer", ENTRY_DESCRIPTION + " U.S. citizens only.", "New York, NY"
        )
        assert result.category == "likely_ineligible"
        assert "citizenship_or_permanent_residency_required" in signals(result)

    def test_missing_sponsorship_language_does_not_reject(self) -> None:
        result = score_job("Software Engineer, New Grad", ENTRY_DESCRIPTION, "New York, NY")
        assert result.category == "strong_match"
        assert not result.hard_blockers

    def test_no_sponsorship_statement_blocks(self) -> None:
        result = score_job(
            "Software Engineer, New Grad",
            ENTRY_DESCRIPTION + " We do not offer visa sponsorship.",
            "New York, NY",
        )
        assert result.category == "likely_ineligible"
        assert "explicit_no_sponsorship" in signals(result)

    def test_2027_only_cohort_blocks(self) -> None:
        result = score_job(
            "Software Engineer, New Grad",
            "For students graduating in 2027. You will write Python.",
            "New York, NY",
        )
        assert result.category == "likely_ineligible"
        assert "incompatible_graduation_window" in signals(result)

    def test_internship_requiring_enrollment_is_blocked(self) -> None:
        result = score_job(
            "Software Engineering Intern",
            "You must be returning to school after the internship. Write Python daily.",
            "New York, NY",
        )
        assert result.category == "likely_ineligible"

    def test_non_us_location_blocks(self) -> None:
        result = score_job("Software Engineer, New Grad", ENTRY_DESCRIPTION, "London, United Kingdom")
        assert result.category == "likely_ineligible"
        assert "outside_united_states" in signals(result)

    def test_non_software_role_scores_zero(self) -> None:
        result = score_job("Account Executive", "Close deals and hit quota.", "New York, NY")
        assert result.score == 0
        assert not result.is_software_role
        assert result.category == "likely_ineligible"

    def test_score_is_always_clamped_to_the_documented_range(self) -> None:
        best = score_job(
            "New Grad Software Engineer I, Early Career",
            ENTRY_DESCRIPTION + " Graduating in 2026. Visa sponsorship is available.",
            "New York, NY",
        )
        assert 0 <= best.score <= 100
        worst = score_job(
            "Senior Staff Principal Engineering Manager",
            "Requires 10+ years. U.S. citizens only. Active TS/SCI clearance is required.",
            "London, United Kingdom",
        )
        assert worst.score == 0

    def test_reasons_are_machine_readable_and_not_double_counted(self) -> None:
        result = score_job("Senior Staff Software Engineer", ENTRY_DESCRIPTION, "New York, NY")
        reasons = [reason.as_dict() for reason in result.reasons]
        assert all({"signal", "delta"} <= set(reason) for reason in reasons)
        seniority_reasons = [
            reason for reason in reasons if reason["signal"] in {"senior_title", "staff_title"}
        ]
        assert len(seniority_reasons) == 1, "only the strongest seniority penalty should apply"

    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (100, "strong_match"),
            (60, "strong_match"),
            (59, "possible_match"),
            (30, "possible_match"),
            (29, "weak_match"),
            (0, "weak_match"),
        ],
    )
    def test_category_thresholds(self, score: int, expected: str) -> None:
        assert categorize_score(score) == expected

    def test_hard_blocker_overrides_category(self) -> None:
        assert categorize_score(95, hard_ineligible=True) == "likely_ineligible"


class TestEvidenceDrivenScoring:
    """The regressions that motivated separating classification from suitability.

    Every case here was checked against the live database first; the wording is
    taken from real postings.
    """

    def test_generic_swe_with_no_experience_evidence_stays_weak(self) -> None:
        for title in (
            "Software Engineer",
            "Software Engineer, Core Platform",
            "Software Engineer, Infrastructure",
            "Full Stack Engineer",
            "Product Engineer",
        ):
            result = score_job(
                title,
                "You will build and maintain services in Python and Go, own features "
                "end to end, and work closely with product.",
                "New York, NY",
            )
            assert result.category == "weak_match", title
            assert result.score < 30, title

    def test_zero_to_two_years_is_a_strong_match(self) -> None:
        result = score_job(
            "Software Engineer",
            "Requirements: 0-2 years of professional software engineering experience.",
            "New York, NY",
        )
        assert result.category == "strong_match"
        assert result.evidence_confidence == "high"
        assert "zero_to_two_years" in signals(result)

    def test_three_plus_preferred_is_not_a_blocker_but_earns_nothing(self) -> None:
        result = score_job(
            "Software Engineer",
            "3+ years of experience preferred. You will build backend services.",
            "New York, NY",
        )
        assert result.category == "weak_match"
        assert not result.hard_blockers
        assert result.experience_category == "three_plus_required"
        assert "preferred" in result.experience_summary

    def test_three_plus_required_is_penalized_and_scores_zero(self) -> None:
        result = score_job(
            "Software Engineer",
            "Requirements: 3+ years of professional software engineering experience.",
            "New York, NY",
        )
        assert result.category == "weak_match"
        assert result.score == 0
        assert "three_plus_required" in signals(result)

    def test_a_senior_title_survives_misleading_junior_wording(self) -> None:
        """Body text about mentoring juniors must not read as an entry-level role."""

        result = score_job(
            "Senior Software Engineer",
            "You will mentor junior engineers and support our new grad cohort. "
            "We welcome recent graduates to our internship program. 0-2 years.",
            "New York, NY",
        )
        assert result.category == "likely_ineligible"
        assert "senior_title" in signals(result)
        assert result.score <= 20, "a blocked posting must not outrank real matches"

    def test_a_new_grad_title_with_a_2027_cohort_is_ineligible(self) -> None:
        result = score_job(
            "Software Engineer (2027 University Grad)",
            "Join our team to build backend services in Python.",
            "New York, NY",
        )
        assert result.category == "likely_ineligible"
        assert "incompatible_graduation_window" in signals(result)

    @pytest.mark.parametrize(
        "title",
        ["Software Engineer, New Grad", "Early Career Software Engineer", "Software Engineer I",
         "Junior Backend Engineer", "Associate Software Engineer"],
    )
    def test_explicit_entry_titles_reach_strong_on_their_own(self, title: str) -> None:
        result = score_job(title, "You will build services in Python.", "New York, NY")
        assert result.category == "strong_match", title
        assert result.evidence_confidence == "high", title

    def test_two_plus_years_is_outside_the_new_grad_target(self) -> None:
        """2+ years is not a new-grad role, and the score has to say so.

        It is still shown rather than blocked: the requirement is real but a
        strong graduate can sometimes argue past it.
        """

        result = score_job(
            "Software Engineer",
            "Requirements: 2+ years of professional experience.",
            "New York, NY",
        )
        assert result.category == "weak_match"
        assert result.experience_category == "two_plus_years"
        assert not result.hard_blockers

    def test_location_alone_cannot_create_a_match(self) -> None:
        """NYC is a preference, not evidence of entry-level suitability."""

        result = score_job("Software Engineer", "Build things.", "New York, NY")
        assert result.category == "weak_match"
        assert result.score <= 10


class TestAllLevelsPostings:
    LADDER_FROM_ASSOCIATE = (
        "We are hiring across multiple experience levels, including: Associate Software "
        "Engineer, Software Engineer, Senior Software Engineer, Staff Software Engineer. "
        "Your title and level will be determined based on your experience and scope."
    )

    def test_all_levels_including_entry_scores_up(self) -> None:
        result = score_job(
            "Software Engineer - All Levels", self.LADDER_FROM_ASSOCIATE, "New York, NY"
        )
        assert "all_levels_includes_entry" in signals(result)
        assert result.category == "possible_match"

    def test_all_levels_excluding_entry_is_downgraded(self) -> None:
        result = score_job(
            "Software Engineer, Backend (All Levels)",
            "We are looking for a Software Engineer (Mid-level to Senior) to build "
            "scalable services.",
            "New York, NY",
        )
        assert "all_levels_excludes_entry" in signals(result)
        assert result.category == "weak_match"

    def test_required_years_override_an_entry_level_ladder(self) -> None:
        """Allen Control lists Associate but still demands three years."""

        result = score_job(
            "Motor Drive Firmware Engineer - All Levels",
            self.LADDER_FROM_ASSOCIATE
            + " What You'll Need: 3+ years of experience in embedded firmware development.",
            "New York, NY",
        )
        assert "all_levels_excludes_entry" in signals(result)
        assert result.category == "weak_match"

    def test_an_undetermined_all_levels_posting_stays_weak_and_uncertain(self) -> None:
        """An 'All Levels' posting with nothing said about seniority."""

        result = score_job(
            "Software Engineer, Core Platform (All Levels)",
            "You will build complex software systems that are performant, reliable and "
            "scalable, and contribute to the technical direction of the platform.",
            "New York, NY",
        )
        assert "all_levels_unspecified_range" in signals(result)
        assert result.category == "weak_match"
        assert result.uncertain
        assert any("multiple levels" in warning for warning in result.warnings)

    def test_an_all_levels_posting_naming_new_grads_counts_as_entry(self) -> None:
        result = score_job(
            "Product Engineer (all levels)",
            "We hire at all levels: our team has new grads and product leaders with "
            "20 years experience.",
            "New York, NY",
        )
        assert "all_levels_includes_entry" in signals(result)
        assert not result.hard_blockers, "team tenure prose is not a 20-year requirement"


class TestEvidenceConfidence:
    def test_confidence_reflects_the_best_positive_evidence(self) -> None:
        high = score_job("Software Engineer, New Grad", "Build services.", "New York, NY")
        medium = score_job(
            "Software Engineer", "Requirements: 2+ years of experience.", "New York, NY"
        )
        low = score_job("Software Engineer", "Build services.", "New York, NY")

        assert (high.evidence_confidence, medium.evidence_confidence, low.evidence_confidence) == (
            "high",
            "medium",
            "low",
        )

    def test_low_confidence_matches_are_marked_uncertain(self) -> None:
        result = score_job("Software Engineer", "Build services.", "New York, NY")
        assert result.uncertain

    def test_a_low_confidence_bonus_does_not_raise_confidence(self) -> None:
        result = score_job(
            "Software Engineer",
            "A Bachelor's degree in Computer Science is required.",
            "New York, NY",
        )
        assert "bachelors_cs" in signals(result)
        assert result.evidence_confidence == "low"


class TestNormalizedJobAnalysis:
    def _job(self, **overrides) -> NormalizedJob:
        values = {
            "source": "ashby",
            "source_company_slug": "acme",
            "source_job_id": "job-1",
            "company_name": "Acme",
            "title": "Software Engineer, New Grad",
            "description_text": ENTRY_DESCRIPTION,
            "location": "New York, NY",
        }
        values.update(overrides)
        return NormalizedJob(**values)

    def test_analysis_attaches_persistable_fields(self) -> None:
        job = self._job()
        result = analyze_normalized_job(job)
        assert job.is_software is True
        assert job.search_score == result.score
        assert job.match_category == result.category == "strong_match"
        assert job.eligibility_category == job.match_category
        assert job.authorization_flag == "unspecified"
        assert all("signal" in reason for reason in job.score_reasons)

    def test_attach_false_leaves_the_job_untouched(self) -> None:
        job = self._job()
        analyze_normalized_job(job, attach=False)
        assert job.search_score is None
        assert job.match_category is None

    def test_structured_address_country_is_used_for_location_rules(self) -> None:
        job = self._job(location="Waterloo", location_context="Waterloo, Ontario, Canada")
        analyze_normalized_job(job)
        assert job.match_category == "likely_ineligible"
        assert "outside_us" in job.eligibility_flags
