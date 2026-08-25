"""Structured experience evidence, and the unknown-vs-negative distinction.

The single most important property tested here: a posting that never mentions
experience must land in ``experience_unknown`` and must never be treated as if
it had asked for too much.  Those are different facts about the world and the
tool has to keep them apart.
"""

from __future__ import annotations

import pytest

from jobfinder.eligibility import extract_year_requirements
from jobfinder.experience import (
    EXPERIENCE_CATEGORIES,
    EXPERIENCE_UNKNOWN,
    EXPLICIT_ENTRY_LEVEL,
    EXPLICIT_NEW_GRAD,
    EXPLICIT_ZERO_EXPERIENCE,
    HARD_BLOCKER,
    ONE_PLUS_YEAR,
    ONE_TO_TWO_YEARS,
    RECENT_GRADUATE,
    THREE_PLUS_REQUIRED,
    TWO_PLUS_YEARS,
    ZERO_TO_ONE_YEAR,
    ZERO_TO_TWO_YEARS,
    classify_experience,
)


def classify(title: str, description: str = ""):
    return classify_experience(title, description, extract_year_requirements(description))


class TestUnknownIsNotNegative:
    def test_a_silent_posting_is_unknown(self) -> None:
        evidence = classify(
            "Software Engineer",
            "You will build and ship backend services with a small team.",
        )
        assert evidence.category == EXPERIENCE_UNKNOWN
        assert evidence.known is False
        assert evidence.delta == 0
        assert evidence.confidence == "low"
        assert evidence.summary == "Not stated"

    def test_unknown_says_why_without_implying_rejection(self) -> None:
        evidence = classify("Software Engineer", "Build things.")
        assert "no explicit experience requirement" in evidence.detail.lower()
        assert "years" not in evidence.summary.lower()

    def test_unknown_and_too_senior_are_different_categories(self) -> None:
        unknown = classify("Software Engineer", "Build things.")
        negative = classify("Software Engineer", "Requires 5+ years of experience.")
        assert unknown.category != negative.category
        assert unknown.known is False and negative.known is True
        assert unknown.delta == 0 and negative.delta < 0

    def test_unknown_is_never_a_hard_blocker(self) -> None:
        assert classify("Software Engineer", "Build things.").hard_blocker is False


class TestExperienceBands:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Requires 0-1 years of experience.", ZERO_TO_ONE_YEAR),
            ("Requires 0-2 years of experience.", ZERO_TO_TWO_YEARS),
            ("Requires 1-2 years of experience.", ONE_TO_TWO_YEARS),
            ("Requires 1+ years of experience.", ONE_PLUS_YEAR),
            ("Requires 2+ years of experience.", TWO_PLUS_YEARS),
            ("Requires 3+ years of experience.", THREE_PLUS_REQUIRED),
            ("Requires 5+ years of experience.", HARD_BLOCKER),
        ],
    )
    def test_each_band_maps_to_its_own_category(self, text: str, expected: str) -> None:
        assert classify("Software Engineer", text).category == expected

    def test_the_primary_target_scores_well_and_the_rest_do_not(self) -> None:
        target = classify("Software Engineer", "Requires 0-1 years of experience.")
        weaker = classify("Software Engineer", "Requires 1+ years of experience.")
        outside = classify("Software Engineer", "Requires 2+ years of experience.")
        negative = classify("Software Engineer", "Requires 3+ years of experience.")
        assert target.delta > weaker.delta > outside.delta > negative.delta

    def test_two_plus_years_is_outside_the_target(self) -> None:
        evidence = classify("Software Engineer", "Requires 2+ years of experience.")
        assert evidence.delta < 0
        assert not evidence.primary_target
        assert evidence.hard_blocker is False

    def test_three_plus_is_strongly_negative_with_high_confidence(self) -> None:
        evidence = classify("Software Engineer", "Requires 3+ years of experience.")
        assert evidence.delta <= -50
        assert evidence.confidence == "high"
        assert "exceeds" in evidence.detail

    def test_four_plus_is_a_hard_blocker(self) -> None:
        assert classify("Software Engineer", "Requires 4+ years.").hard_blocker is True

    def test_preferred_costs_far_less_than_required(self) -> None:
        required = classify("Software Engineer", "Requires 3+ years of experience.")
        preferred = classify("Software Engineer", "3+ years of experience preferred.")
        assert preferred.delta > required.delta
        assert preferred.requirement == "preferred"
        assert preferred.hard_blocker is False


class TestExplicitWording:
    def test_a_new_grad_title_is_explicit_new_grad(self) -> None:
        evidence = classify("Software Engineer, New Grad", "Build services.")
        assert evidence.category == EXPLICIT_NEW_GRAD
        assert evidence.confidence == "high"
        assert evidence.primary_target

    @pytest.mark.parametrize(
        "title",
        ["Junior Software Engineer", "Associate Software Engineer", "Software Engineer I"],
    )
    def test_entry_level_titles_are_explicit_entry_level(self, title: str) -> None:
        assert classify(title, "Build services.").category == EXPLICIT_ENTRY_LEVEL

    def test_recent_graduates_welcome_in_the_body(self) -> None:
        evidence = classify(
            "Software Engineer", "We welcome recent graduates to apply for this role."
        )
        assert evidence.category == RECENT_GRADUATE

    def test_no_experience_required_is_explicit_zero(self) -> None:
        evidence = classify(
            "Software Engineer", "No prior professional experience is required."
        )
        assert evidence.category == EXPLICIT_ZERO_EXPERIENCE
        assert evidence.delta >= 60

    def test_a_new_grad_title_outranks_a_soft_numeric_band(self) -> None:
        """This is the case where friendly wording legitimately wins."""

        evidence = classify(
            "Software Engineer, New Grad", "Requires 1+ years of experience."
        )
        assert evidence.category == EXPLICIT_NEW_GRAD
        assert "1+ years" in evidence.detail

    def test_a_three_plus_requirement_beats_friendly_wording(self) -> None:
        """A recruiter screens on the number, so the number wins — visibly."""

        evidence = classify(
            "New Grad Software Engineer", "Requires 3+ years of professional experience."
        )
        assert evidence.category == THREE_PLUS_REQUIRED
        assert "disagree" in evidence.detail

    def test_a_senior_sounding_body_does_not_invent_a_requirement(self) -> None:
        evidence = classify(
            "Software Engineer",
            "Our team has new grads and product leaders with 20 years experience.",
        )
        assert evidence.category != HARD_BLOCKER


class TestCategoryHygiene:
    def test_every_category_is_registered(self) -> None:
        samples = [
            classify("Software Engineer, New Grad", "Build services."),
            classify("Software Engineer", "Requires 0-1 years."),
            classify("Software Engineer", "Requires 5+ years."),
            classify("Software Engineer", "Build services."),
        ]
        for evidence in samples:
            assert evidence.category in EXPERIENCE_CATEGORIES

    def test_every_category_has_a_label_and_a_filter_bucket(self) -> None:
        for category in EXPERIENCE_CATEGORIES:
            evidence = classify_experience("Software Engineer", "", ())
            assert category in EXPERIENCE_CATEGORIES
            assert evidence.filter_bucket
        from jobfinder.experience import CATEGORY_LABELS, FILTER_BUCKETS

        assert set(EXPERIENCE_CATEGORIES) <= set(CATEGORY_LABELS)
        assert set(EXPERIENCE_CATEGORIES) <= set(FILTER_BUCKETS)
