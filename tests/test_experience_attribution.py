"""Whose experience is it?

A duration only becomes a requirement when it belongs to the applicant.  Job
descriptions are full of durations that belong to somebody else — founders,
advisors, the leadership team — and reading those as requirements turned an
explicit new-grad posting into a hard blocker.

The fixture below is a synthetic posting with the same structure as the real
one that exposed the bug.  Its whole description contains exactly one "years"
phrase, and that phrase is about the founding team.
"""

from __future__ import annotations

import pytest

from jobfinder.eligibility import (
    analyze_eligibility,
    experience_attribution,
    extract_year_requirements,
)
from jobfinder.experience import classify_experience
from jobfinder.scoring import score_job


CONTOSO_TITLE = "Software Engineer, Backend New Graduate"
CONTOSO_DESCRIPTION = """Software Engineer — New Graduate

We're seeking recent and upcoming graduates to join our engineering team.

As a New Graduate Software Engineer, you'll help build and grow the backend
systems behind our alert triage product, working in Python and Go on
services, data pipelines and APIs.

Your work reaches customers quickly. You'll talk with the teams who use the
product, learn what they need, and see how your changes help them.

Contoso Security's founding team brings more than 30 years of cybersecurity
experience from companies including Litware, Northwind Traders, Tailspin Toys
and Woodgrove Bank.

What we're looking for
- Recently completed or currently completing a bachelor's or master's degree in
  Computer Science or a related technical field.
- Experience with a programming language such as Python or Go, through
  coursework, research, internships, or personal projects.
- Experience with backend services, cloud infrastructure, security tooling, or
  machine learning is helpful but not required.
"""


class TestContosoSecurityRegression:
    """A synthetic reproduction of the false negative that prompted this fix."""

    def test_the_founding_team_boast_is_not_a_requirement(self) -> None:
        assert extract_year_requirements(CONTOSO_DESCRIPTION) == ()

    def test_the_only_duration_is_attributed_to_the_founders(self) -> None:
        found = experience_attribution(CONTOSO_DESCRIPTION)
        assert len(found) == 1, "the posting states exactly one duration"
        assert found[0]["phrase"].strip().startswith("30")
        assert found[0]["applicant"] is False
        assert "founding team" in found[0]["clause"]

    def test_no_seniority_or_four_plus_flag_survives(self) -> None:
        analysis = analyze_eligibility(CONTOSO_TITLE, CONTOSO_DESCRIPTION, "Palo Alto")
        assert "four_plus_years_required" not in analysis.flags
        assert "senior_role" not in analysis.flags
        assert analysis.seniority.likely_senior is False

    def test_it_is_recognised_as_an_explicit_new_grad_role(self) -> None:
        evidence = classify_experience(
            CONTOSO_TITLE,
            CONTOSO_DESCRIPTION,
            extract_year_requirements(CONTOSO_DESCRIPTION),
        )
        assert evidence.category == "explicit_new_grad"
        assert evidence.confidence == "high"
        assert evidence.hard_blocker is False

    def test_the_scored_result_is_a_high_confidence_new_grad_match(self) -> None:
        result = score_job(CONTOSO_TITLE, CONTOSO_DESCRIPTION, "Palo Alto (Hybrid)")
        assert result.experience_category == "explicit_new_grad"
        assert result.evidence_confidence == "high"
        assert result.entry_level_score >= 60
        assert result.category == "strong_match"
        assert not result.hard_blockers
        assert result.blocker_status != "blocked"

    def test_no_entry_level_evidence_is_never_reported(self) -> None:
        result = score_job(CONTOSO_TITLE, CONTOSO_DESCRIPTION, "Palo Alto (Hybrid)")
        signals = {reason.signal for reason in result.reasons}
        assert "no_entry_level_evidence" not in signals
        assert "explicit_new_grad" in signals

    def test_authorization_is_read_from_the_posting_not_invented(self) -> None:
        """The posting says nothing about sponsorship, so neither do we."""

        analysis = analyze_eligibility(CONTOSO_TITLE, CONTOSO_DESCRIPTION, "Palo Alto")
        assert analysis.work_authorization.status == "unspecified"
        assert not analysis.work_authorization.explicit_blocker


class TestThirdPartyExperienceNeverCounts:
    @pytest.mark.parametrize(
        "text",
        [
            "Our founding team has 30 years of experience.",
            "Our founders bring 25+ years of combined experience.",
            "Our leadership team has 50 years of industry experience.",
            "Together, our engineers have decades of experience.",
            "Backed by operators with 20+ years of experience.",
            "Learn from engineers with 15 years of experience.",
            "Our CEO has 20 years of experience.",
            "Our team has new grads and product leaders with 20 years of experience.",
            "Our advisors have 40 years of combined experience.",
            "The executive team brings 60 years of experience.",
            "Our investors have 30 years of experience backing founders.",
            "Founded in 2017, we have spent 8 years building the platform.",
        ],
    )
    def test_somebody_elses_experience_is_not_a_requirement(self, text: str) -> None:
        assert extract_year_requirements(text) == (), text

    def test_a_third_party_boast_cannot_make_a_posting_senior(self) -> None:
        analysis = analyze_eligibility(
            "Software Engineer, New Graduate",
            "Our founding team brings 30 years of experience. We welcome new graduates.",
            "New York, NY",
        )
        assert "senior_role" not in analysis.flags
        assert "four_plus_years_required" not in analysis.flags


class TestApplicantRequirementsAlwaysCount:
    @pytest.mark.parametrize(
        "text",
        [
            "You have 5+ years of software engineering experience.",
            "We require 5+ years of software engineering experience.",
            "Candidates must have at least 5 years of experience.",
            "Minimum 5 years of relevant experience.",
            "Qualifications: 5+ years of backend engineering experience.",
            "We're looking for an engineer with 5+ years of experience.",
        ],
    )
    def test_a_real_requirement_is_still_extracted(self, text: str) -> None:
        found = extract_year_requirements(text)
        assert found, text
        assert found[0].min_years == 5, text
        assert found[0].required, text

    @pytest.mark.parametrize(
        "text",
        [
            "Our ideal staff security engineer will have: 5+ years of security experience.",
            "We're looking for people who have 3+ years of engineering experience.",
            "You have a degree in Computer Science, backed by 5+ years of experience.",
            "Working with our engineers, you must have 5+ years of experience.",
            "Our best early-stage engineers share a background such as 2+ years of experience.",
        ],
    )
    def test_company_words_nearby_do_not_suppress_a_requirement(self, text: str) -> None:
        """Over-suppression is as damaging as the false positive it prevents."""

        assert extract_year_requirements(text), text

    def test_the_clause_subject_decides_not_the_nearest_word(self) -> None:
        """'You have ..., backed by 5+ years' is a requirement; the subject is you."""

        assert extract_year_requirements(
            "You have a degree, backed by 5+ years of experience."
        )
        assert not extract_year_requirements(
            "Backed by operators with 5+ years of experience."
        )

    def test_a_boast_does_not_shadow_a_later_requirement(self) -> None:
        found = extract_year_requirements(
            "Our founding team brings 30 years of experience. "
            "Requirements: 5+ years of Python."
        )
        assert [item.min_years for item in found] == [5]

    def test_a_bullet_boundary_stops_attribution_leaking(self) -> None:
        """One boast at the top of a list must not capture every number below."""

        found = extract_year_requirements(
            "Our leadership team has 40 years of experience - 5+ years of Go required"
        )
        assert [item.min_years for item in found] == [5]


class TestEntryLevelEvidenceStaysIndependent:
    """A negative experience signal must never erase positive evidence."""

    CONTRADICTORY = (
        "We are seeking new graduates for this role. "
        "Requirements: you must have 5+ years of professional backend experience."
    )

    def test_the_number_still_decides_the_category(self) -> None:
        result = score_job("Software Engineer, New Graduate", self.CONTRADICTORY, "NYC")
        assert result.experience_category == "hard_blocker"
        assert result.category == "likely_ineligible"

    def test_the_entry_level_wording_is_preserved(self) -> None:
        result = score_job("Software Engineer, New Graduate", self.CONTRADICTORY, "NYC")
        signals = {reason.signal for reason in result.reasons}
        assert "explicit_new_grad" in signals

    def test_it_is_never_called_no_entry_level_evidence(self) -> None:
        result = score_job("Software Engineer, New Graduate", self.CONTRADICTORY, "NYC")
        signals = {reason.signal for reason in result.reasons}
        assert "no_entry_level_evidence" not in signals

    def test_the_contradiction_is_stated(self) -> None:
        result = score_job("Software Engineer, New Graduate", self.CONTRADICTORY, "NYC")
        assert any("contradict" in warning.lower() for warning in result.warnings)

    def test_a_posting_with_neither_still_says_no_evidence(self) -> None:
        result = score_job(
            "Software Engineer",
            "You will build services. Requirements: 5+ years of experience.",
            "NYC",
        )
        signals = {reason.signal for reason in result.reasons}
        assert "no_entry_level_evidence" in signals
