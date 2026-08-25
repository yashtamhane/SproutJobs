"""Tests for semantic duplicate flagging (which must never merge records)."""

from __future__ import annotations

from jobfinder.dedup import (
    find_duplicate_groups,
    semantic_duplicate_key,
    source_identity,
)


DESCRIPTION = (
    "We are hiring a new grad software engineer to build and maintain backend services. "
    "You will write Python, review code, design APIs, and ship features weekly alongside "
    "a small product team that values curiosity, ownership, and thoughtful engineering."
)


def record(job_id: str, **overrides) -> dict:
    values = {
        "source": "ashby",
        "source_company_slug": "acme",
        "source_job_id": job_id,
        "company_name": "Acme",
        "title": "Software Engineer, New Grad",
        "department": "Engineering",
        "location": "New York, NY",
        "description_text": DESCRIPTION,
        "job_url": f"https://jobs.ashbyhq.com/acme/{job_id}",
        "apply_url": f"https://jobs.ashbyhq.com/acme/{job_id}/application",
    }
    values.update(overrides)
    return values


class TestDuplicateKey:
    def test_key_ignores_location_so_variants_can_group(self) -> None:
        assert semantic_duplicate_key("acme", "Software Engineer", "New York") == (
            semantic_duplicate_key("acme", "Software Engineer", "Austin")
        )

    def test_key_separates_different_companies(self) -> None:
        assert semantic_duplicate_key("acme", "Software Engineer") != semantic_duplicate_key(
            "globex", "Software Engineer"
        )

    def test_requisition_suffixes_do_not_split_a_repost(self) -> None:
        assert semantic_duplicate_key("acme", "Software Engineer req 12345") == (
            semantic_duplicate_key("acme", "Software Engineer")
        )


class TestGrouping:
    def test_matching_titles_alone_are_not_duplicates(self) -> None:
        groups = find_duplicate_groups(
            [
                record("a", description_text="Work on our billing platform in Kotlin."),
                record("b", description_text="Own the iOS client and its release process."),
            ]
        )
        assert groups == ()

    def test_a_repost_under_a_new_id_is_flagged(self) -> None:
        groups = find_duplicate_groups([record("old"), record("new")])
        assert len(groups) == 1
        assert groups[0].size == 2
        assert "same_description" in groups[0].reasons

    def test_location_variants_group_as_one_review_item(self) -> None:
        groups = find_duplicate_groups(
            [
                record("nyc", location="New York, NY"),
                record("sf", location="San Francisco, CA"),
                record("aus", location="Austin, TX"),
            ]
        )
        assert len(groups) == 1
        assert groups[0].size == 3

    def test_the_same_url_seen_twice_is_high_confidence(self) -> None:
        groups = find_duplicate_groups(
            [
                record("one", job_url="https://jobs.ashbyhq.com/acme/shared/"),
                record("two", job_url="https://jobs.ashbyhq.com/acme/shared"),
            ]
        )
        assert groups[0].confidence == "high"
        assert "same_canonical_job_url" in groups[0].reasons

    def test_the_same_source_record_twice_is_not_a_duplicate_pair(self) -> None:
        groups = find_duplicate_groups([record("a"), record("a")])
        assert groups == ()

    def test_original_records_are_preserved_and_never_merged(self) -> None:
        first, second = record("old"), record("new")
        groups = find_duplicate_groups([first, second])

        assert [id(item) for item in groups[0].records] == [id(first), id(second)]
        assert groups[0].identities == (("ashby", "old"), ("ashby", "new"))
        assert first["source_job_id"] == "old" and second["source_job_id"] == "new"

    def test_group_serialization_carries_identities_not_payloads(self) -> None:
        payload = find_duplicate_groups([record("old"), record("new")])[0].as_dict()
        assert payload["size"] == 2
        assert payload["source_identities"] == [["ashby", "old"], ["ashby", "new"]]
        assert "description_text" not in str(payload)


def test_source_identity_reads_common_field_names() -> None:
    assert source_identity({"source": "ashby", "source_job_id": "x"}) == ("ashby", "x")
    assert source_identity({"ashby_job_id": "y"}) == ("ashby", "y")
