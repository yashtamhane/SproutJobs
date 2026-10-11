"""Recall and precision of the sponsorship / work-authorization parser.

Detection is *scoped* rather than adjacent: a refusal counts when it governs a
sponsorship object inside the same sentence.  That is what the live corpus
demanded — real postings put eighty-plus characters between "we are unable to"
and the word they are refusing.

The negative cases matter as much as the positive ones.  Nine live boards say
"we can't successfully sponsor for every role and every candidate, but if we
make you an offer we'll make every reasonable effort" — a sponsorship-friendly
sentence that a naive "can't sponsor" rule would read as a refusal.
"""

from __future__ import annotations

import pytest

from jobfinder.eligibility import analyze_eligibility, detect_work_authorization
from jobfinder.normalize import html_to_text
from jobfinder.scoring import score_job


# A synthetic sentence with the shape that broke the adjacency-based parser,
# which scored such a posting as a strong match while reporting "Sponsorship
# not mentioned": the refusal opens the sentence, and the word it refuses
# arrives only after a long intervening clause.
LONG_GAP_REFUSAL = (
    "For the time being, we are unable to offer employment to people who live "
    "outside of the United States and/or to anyone who requires sponsorship for "
    "an employment visa (such as H-1B status). Thank you for reading this far "
    "and for thinking of our team while you weigh your next move."
)


class TestLongGapRefusalRegression:
    """A synthetic reproduction of the false negative that prompted this audit."""

    def test_the_long_gap_sentence_is_a_blocker(self) -> None:
        analysis = detect_work_authorization(LONG_GAP_REFUSAL)
        assert analysis.explicit_blocker
        assert "no_sponsorship" in analysis.flags
        assert analysis.status == "likely_ineligible"

    def test_it_is_never_reported_as_unmentioned(self) -> None:
        assert detect_work_authorization(LONG_GAP_REFUSAL).status != "unspecified"

    def test_the_refusal_survives_the_intervening_clause(self) -> None:
        """105 characters separate 'unable to offer employment' from 'sponsorship' here."""

        gap = LONG_GAP_REFUSAL.lower().index("sponsorship") - LONG_GAP_REFUSAL.lower().index(
            "unable to offer employment"
        )
        assert gap > 80, "the fixture must keep the long gap that broke the old parser"
        assert detect_work_authorization(LONG_GAP_REFUSAL).explicit_blocker

    def test_entry_level_fit_is_preserved_while_eligibility_blocks(self) -> None:
        """Fit and eligibility are separate questions; both must stay true.

        The Engineer I title still earns its fit, and the sponsorship
        restriction still caps the ranked score.  ``Software Engineer I`` and a
        one-line duty sentence stand in for a full posting so that the software
        classifier, which is a different rule under test elsewhere, passes.
        """

        result = score_job(
            "Software Engineer I",
            "You will build and maintain backend services in Python. " + LONG_GAP_REFUSAL,
            "Sacramento, CA",
        )
        assert result.is_software_role
        assert result.entry_level_score >= 60, "the title still says Engineer I"
        assert result.experience_category == "explicit_entry_level"
        assert result.blocker_status == "blocked"
        assert result.category == "likely_ineligible"
        assert result.score <= 20, "a blocked posting must not outrank real matches"


class TestSponsorshipRefusalVariants:
    @pytest.mark.parametrize(
        "text",
        [
            "We are unable to offer sponsorship for this role.",
            "We are unable to provide sponsorship.",
            "We are unable to sponsor at this time.",
            "We cannot sponsor visas.",
            "We will not sponsor employment visas.",
            "We do not sponsor visas.",
            "Sponsorship is not available for this position.",
            "Sponsorship unavailable.",
            "Visa sponsorship: not available",
            "Candidates requiring sponsorship cannot be considered.",
            "We are unable to employ candidates who require sponsorship.",
            "We are unable to offer employment to individuals who require sponsorship.",
            "This role is not open to those who require sponsorship for employment visa status.",
            "H-1B sponsorship unavailable.",
            "Employment visa sponsorship unavailable.",
            "This position is generally not eligible for new visa sponsorship.",
            "This position is not eligible for immigration sponsorship.",
            "Please note our company does not provide visa support or sponsorship for employees.",
            "Unfortunately we are not able to offer any visa support at this time.",
            "We're not offering visa sponsorship at this time.",
            "We are able to support H-1B transfers but are not initiating new visa sponsorships.",
        ],
    )
    def test_each_refusal_variant_blocks(self, text: str) -> None:
        analysis = detect_work_authorization(text)
        assert analysis.explicit_blocker, text
        assert "no_sponsorship" in analysis.flags, text

    @pytest.mark.parametrize(
        "text",
        [
            "You must not require sponsorship now or in the future.",
            "There is no current or future sponsorship for this position.",
            "You must be authorized to work in the US without current or future sponsorship.",
            "Applicants must be authorized to work without sponsorship.",
            "You must be work-authorized in the United States without the need for "
            "current or future employer sponsorship.",
            "Applicants who require sponsorship will not be considered.",
        ],
    )
    def test_each_requirement_variant_blocks(self, text: str) -> None:
        analysis = detect_work_authorization(text)
        assert analysis.explicit_blocker, text
        assert "must_not_require_sponsorship" in analysis.flags, text

    def test_indirect_employment_eligibility_wording_is_caught(self) -> None:
        """Requirement 4: the restriction need not use the words "we sponsor"."""

        for text in (
            "We are unable to offer employment to anyone who requires visa sponsorship.",
            "Individuals needing visa sponsorship are not eligible for this role.",
            "Candidates must possess the legal right to work in their intended work "
            "location, as we are currently unable to sponsor or transfer employment "
            "visas for any country, including the United States.",
        ):
            assert detect_work_authorization(text).explicit_blocker, text


class TestNoFalsePositives:
    """Sentences that mention sponsorship without restricting anyone."""

    @pytest.mark.parametrize(
        "text",
        [
            "We provide visa sponsorship.",
            "Sponsorship may be available.",
            "We have sponsored H-1B employees.",
            "Candidates requiring sponsorship are welcome.",
            "We are open to visa sponsorship for exceptional candidates.",
            "US work authorization is preferred but not required, can sponsor visas.",
            "We consider applicants with or without sponsorship needs.",
            "Verification of U.S. employment eligibility is required.",
            "We offer relocation assistance and immigration benefits including green "
            "card support after one year.",
            "Company sponsored 401(k) plan so you can save pre-tax for the long term.",
            "If you prefer not to have your application assessed using AI/ML features, "
            "you may opt out by completing this form.",
        ],
    )
    def test_benign_sponsorship_language_never_blocks(self, text: str) -> None:
        assert not detect_work_authorization(text).explicit_blocker, text

    @pytest.mark.parametrize(
        "text",
        [
            "We are an equal opportunity employer and do not discriminate on the basis "
            "of race, color, religion, sex, sexual orientation, gender identity, "
            "national origin, citizenship or immigration status, or age.",
            "We welcome all individuals and do not discriminate on the basis of gender "
            "identity, ethnicity, disability, citizenship, or veteran status.",
            "All qualified applicants will receive consideration without regard to "
            "race, national origin, or citizenship status.",
        ],
    )
    def test_equal_opportunity_boilerplate_never_blocks(self, text: str) -> None:
        """The largest false-positive risk in the corpus: ~100 live occurrences."""

        assert not detect_work_authorization(text).explicit_blocker, text

    @pytest.mark.parametrize(
        "text",
        [
            "While we can't guarantee success for every candidate or role, if you're "
            "the right fit, we're committed to working through the visa process together.",
            "We can't successfully sponsor for every role and every candidate, but if "
            "we make you an offer we'll make every reasonable effort, and we retain an "
            "immigration law firm to help.",
            "While we cannot guarantee your visa, we have historically been successful "
            "in sponsoring candidates from all over the world.",
        ],
    )
    def test_hedged_but_friendly_sponsorship_text_never_blocks(self, text: str) -> None:
        """These read like refusals word-for-word but offer sponsorship."""

        assert not detect_work_authorization(text).explicit_blocker, text

    def test_a_visa_type_list_is_not_a_refusal(self) -> None:
        text = "This includes, but is not limited to: F1-OPT, F1-CPT, H-1B, TN, L-1, J1, etc."
        assert not detect_work_authorization(text).explicit_blocker

    def test_opt_out_is_not_an_opt_visa_exclusion(self) -> None:
        text = "You may opt out of marketing emails at any time."
        analysis = detect_work_authorization(text)
        assert "opt_not_accepted" not in analysis.flags
        assert not analysis.explicit_blocker


class TestDistinctRestrictionCategories:
    """Requirement 5: these must not collapse into one flag."""

    def test_citizenship_is_its_own_category(self) -> None:
        analysis = detect_work_authorization("You must be a United States (U.S.) citizen.")
        assert "citizenship_required" in analysis.flags
        assert "no_sponsorship" not in analysis.flags

    def test_permanent_resident_is_its_own_category(self) -> None:
        analysis = detect_work_authorization("Green card holders only.")
        assert "permanent_resident_only" in analysis.flags

    def test_export_control_is_its_own_category(self) -> None:
        analysis = detect_work_authorization(
            "Due to ITAR requirements, applicants must be a U.S. person as defined by "
            "the International Traffic in Arms Regulations."
        )
        assert "itar_export_control" in analysis.flags
        assert analysis.explicit_blocker

    def test_a_bare_export_control_notice_is_uncertain_not_blocked(self) -> None:
        """13 live postings carry this hedged notice; it bars nobody by itself."""

        analysis = detect_work_authorization(
            "Export control notice: this position may require access to technology or "
            "source code subject to U.S. export control laws, which may require the "
            "company to seek government authorization."
        )
        assert not analysis.explicit_blocker
        assert "export_control_mentioned" in analysis.flags
        assert analysis.status == "uncertain"

    def test_clearance_is_its_own_category(self) -> None:
        analysis = detect_work_authorization("An active Top Secret clearance is required.")
        assert "security_clearance_required" in analysis.flags

    def test_opt_exclusion_is_its_own_category(self) -> None:
        analysis = detect_work_authorization("We are not currently able to engage candidates on OPT.")
        assert "opt_not_accepted" in analysis.flags

    def test_sponsorship_available_is_its_own_state(self) -> None:
        analysis = detect_work_authorization("We offer visa sponsorship for this role.")
        assert analysis.status == "positive"
        assert "sponsorship_available" in analysis.flags
        assert not analysis.explicit_blocker

    def test_silence_stays_unspecified(self) -> None:
        analysis = detect_work_authorization("You will build backend services in Go.")
        assert analysis.status == "unspecified"
        assert not analysis.flags


class TestFormattingRobustness:
    """Requirement 12: punctuation, line breaks, HTML, parentheticals, bullets."""

    def test_a_parenthetical_does_not_break_detection(self) -> None:
        assert detect_work_authorization(
            "We cannot offer sponsorship for employment visa status (e.g., H-1B visa status)."
        ).explicit_blocker

    def test_a_parenthetical_inside_a_citizenship_phrase(self) -> None:
        assert detect_work_authorization(
            "To be considered, you must be a United States (U.S.) citizen."
        ).explicit_blocker

    def test_line_breaks_are_handled(self) -> None:
        assert detect_work_authorization(
            "Additional Information\nAt this time, we are unable to offer employment\n"
            "for those who require sponsorship for employment visa status."
        ).explicit_blocker

    def test_html_converts_before_detection(self) -> None:
        html = (
            "<div><h3>Additional Information</h3><p>At this time, we are "
            "<strong>unable to offer employment</strong> for individuals located "
            "outside of the United States and/or those who require "
            "<em>sponsorship</em> for employment visa status (e.g., H-1B visa "
            "status).</p></div>"
        )
        assert detect_work_authorization(html_to_text(html)).explicit_blocker
        assert detect_work_authorization(html).explicit_blocker

    def test_a_bullet_list_restriction_is_caught(self) -> None:
        assert detect_work_authorization(
            "Requirements:\n"
            "- 2 years of experience\n"
            "- You must be work-authorized in the United States without the need for "
            "current or future employer sponsorship\n"
            "- Strong communication skills"
        ).explicit_blocker

    def test_curly_apostrophes_are_handled(self) -> None:
        assert detect_work_authorization(
            "We’re unable to sponsor or take over sponsorship of employment visas at this time."
        ).explicit_blocker

    def test_a_restriction_split_across_sentences_does_not_leak(self) -> None:
        """A negation in one sentence must not govern a later sentence."""

        analysis = detect_work_authorization(
            "We do not require a cover letter. Visa sponsorship is available for this role."
        )
        assert not analysis.explicit_blocker
        assert analysis.status == "positive"

    def test_a_semicolon_separates_scopes(self) -> None:
        analysis = detect_work_authorization(
            "Remote in a US timezone; US preferred; cannot sponsor visas at this time"
        )
        assert analysis.explicit_blocker


class TestEligibilityIntegration:
    def test_the_blocker_reaches_the_aggregate_analysis(self) -> None:
        analysis = analyze_eligibility(
            "Software Engineer I", LONG_GAP_REFUSAL, "Sacramento, CA"
        )
        assert analysis.likely_ineligible
        assert analysis.authorization_flag == "likely_ineligible"
        assert "no_sponsorship" in analysis.flags


class TestAuditRegressions:
    """Four bugs the live-database audit caught in the first version of this fix.

    Each was a real posting the rewrite had started letting through, and each
    is a different class of mistake — so each keeps its own test.
    """

    def test_a_bare_no_directly_on_the_object_is_a_refusal(self) -> None:
        """Postings write this with no verb at all: "no visa sponsorship."."""

        for text in (
            "Local candidates only. No visa sponsorship.",
            "Must be legally authorized to work in the United States — no visa "
            "sponsorship available.",
            "This is an in-person role for builders. - No visa sponsorship.",
        ):
            analysis = detect_work_authorization(text)
            assert analysis.explicit_blocker, text
            assert "no_sponsorship" in analysis.flags, text

    def test_a_bare_no_elsewhere_is_not_a_refusal(self) -> None:
        """The adjacency requirement is what keeps this from over-firing."""

        analysis = detect_work_authorization(
            "No experience required. Visa sponsorship is available for this role."
        )
        assert not analysis.explicit_blocker
        assert analysis.status == "positive"

    def test_a_curly_apostrophe_reads_the_same_as_a_straight_one(self) -> None:
        straight = detect_work_authorization("Work authorization: We can't sponsor visas at this time.")
        curly = detect_work_authorization("Work authorization: We can’t sponsor visas at this time.")
        assert straight.explicit_blocker and curly.explicit_blocker
        assert straight.flags == curly.flags

    def test_an_em_dash_separates_clauses(self) -> None:
        assert detect_work_authorization(
            "Must be legally authorized to work in the US — no visa sponsorship is available"
        ).explicit_blocker

    def test_a_hedge_elsewhere_does_not_cancel_a_stated_clearance(self) -> None:
        """The conditional test is scoped to the clearance sentence itself."""

        analysis = detect_work_authorization(
            "Active Secret clearance required; willingness and eligibility to obtain a "
            "higher-level clearance if needed. This position may require the ability to "
            "work in a classified environment when necessary."
        )
        assert "security_clearance_required" in analysis.flags
        assert analysis.explicit_blocker

    def test_ability_to_gain_a_clearance_still_counts(self) -> None:
        analysis = detect_work_authorization(
            "Citizenship/Clearance\nPossess or ability to gain and maintain Top Secret "
            "security clearance required"
        )
        assert "security_clearance_required" in analysis.flags

    def test_citizenship_stated_as_a_requirement_noun(self) -> None:
        analysis = detect_work_authorization(
            "Note: Due to the nature of certain government contracts held by this "
            "organization, U.S. citizenship is a requirement for all positions."
        )
        assert "citizenship_required" in analysis.flags
        assert analysis.explicit_blocker

    def test_a_purely_conditional_clearance_stays_uncertain(self) -> None:
        analysis = detect_work_authorization(
            "This position may require the ability to obtain a U.S. government security "
            "clearance if needed."
        )
        assert "security_clearance_required" not in analysis.flags
        assert analysis.status == "uncertain"


class TestNeverClaimsSilenceWrongly:
    """Requirement 10, as an invariant rather than a spot check.

    If a posting says anything at all about sponsorship, visas or work
    authorization, the parser may not report ``unspecified`` — because the
    dashboard renders that as "No sponsorship information found", and saying so
    about a posting that discussed visas is a false statement to the reader.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Harvey does not currently offer visa sponsorship for this role.",
            "Work eligibility: Must be authorized to work in India. Visa sponsorship is "
            "not available for this role.",
            "Benefits: employer-sponsored insurance, medical, dental and vision.",
            "We sponsor H-1B and TN visas.",
            "Applicants must be authorized to work in the United States.",
            "This role involves export-controlled technology.",
            "A security clearance is preferred.",
            "We support green card applications after your first year.",
        ],
    )
    def test_any_authorization_language_leaves_unspecified(self, text: str) -> None:
        assert detect_work_authorization(text).status != "unspecified", text

    def test_a_posting_with_no_such_language_stays_unspecified(self) -> None:
        analysis = detect_work_authorization(
            "You will build backend services in Go, review pull requests, and help "
            "mentor interns. We offer unlimited PTO and a learning budget."
        )
        assert analysis.status == "unspecified"

    def test_the_dashboard_label_follows_the_parser(self) -> None:
        from jobfinder.reporting import _authorization

        assert _authorization("unspecified", [])[0] == "No sponsorship information found"
        assert (
            _authorization("uncertain", ["sponsorship_language_present"])[0]
            == "Sponsorship language present, eligibility uncertain"
        )
        assert _authorization("positive", ["sponsorship_available"])[0] == (
            "Sponsorship appears available"
        )
        assert _authorization("likely_ineligible", ["no_sponsorship"])[0] == (
            "Explicit sponsorship restriction"
        )

    def test_the_old_overclaiming_label_is_gone_everywhere(self) -> None:
        from jobfinder.reporting import _authorization

        for status in ("unspecified", "uncertain", "positive", "likely_ineligible", "none"):
            assert "not mentioned" not in _authorization(status, [])[0].lower()


class TestEveryRestrictionFiltersAsARestriction:
    """A named restriction must also sort under the dashboard's restriction filter."""

    @pytest.mark.parametrize(
        "flag",
        [
            "no_sponsorship",
            "must_not_require_sponsorship",
            "citizenship_required",
            "permanent_resident_only",
            "us_person_required",
            "itar_export_control",
            "permanent_work_authorization_required",
            "opt_not_accepted",
            "security_clearance_required",
        ],
    )
    def test_restriction_state(self, flag: str) -> None:
        from jobfinder.reporting import _authorization

        label, state = _authorization("likely_ineligible", [flag])
        assert state == "restriction", f"{flag} rendered as {label!r} / {state}"
