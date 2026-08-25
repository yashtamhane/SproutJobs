"""U.S.-wide geography tagging.

The point of these tests is that collection is nationwide and that a posting can
belong to several buckets at once.  Anything that silently drops a U.S. job for
being outside New York is a regression.
"""

from __future__ import annotations

import pytest

from jobfinder.geo import (
    GEO_TAGS,
    NEW_JERSEY,
    NYC,
    OTHER_US,
    REMOTE_US,
    analyze_geography,
    geo_tags,
    label_for,
)


class TestGeographyTags:
    @pytest.mark.parametrize(
        "location",
        ["New York, NY", "New York City", "Brooklyn, New York", "NYC", "Manhattan"],
    )
    def test_new_york_locations_are_tagged_nyc(self, location: str) -> None:
        assert NYC in geo_tags(location)

    @pytest.mark.parametrize(
        "location",
        ["Jersey City, NJ", "Hoboken, New Jersey", "Newark, NJ", "Princeton, NJ"],
    )
    def test_new_jersey_locations_are_tagged_new_jersey(self, location: str) -> None:
        assert NEW_JERSEY in geo_tags(location)

    @pytest.mark.parametrize(
        "location",
        [
            "Austin, TX",
            "San Francisco, CA",
            "Seattle, Washington",
            "Denver, Colorado",
            "Chicago, Illinois",
            "Atlanta, GA",
        ],
    )
    def test_other_us_cities_are_kept_not_discarded(self, location: str) -> None:
        """A job in Texas is a real option, not something to hide."""

        result = analyze_geography(location)
        assert result.tags == (OTHER_US,), location
        assert result.us_based is True
        assert result.in_scope

    def test_a_city_without_a_state_uses_the_structured_address(self) -> None:
        """Ashby supplies a country for ~97% of postings; use it."""

        result = analyze_geography(
            "San Francisco", location_context="San Francisco, California, United States"
        )
        assert result.tags == (OTHER_US,)
        assert result.us_based is True

    def test_a_two_city_posting_earns_both_tags(self) -> None:
        result = analyze_geography(
            "New York, NY",
            secondary_locations=["Austin, TX"],
        )
        assert set(result.tags) == {NYC, OTHER_US}

    def test_remote_us_is_its_own_tag_alongside_a_city(self) -> None:
        result = analyze_geography(
            "New York, NY", workplace_type="Remote", location_context="New York, NY, United States"
        )
        assert NYC in result.tags
        assert REMOTE_US in result.tags

    def test_remote_united_states_without_a_city(self) -> None:
        result = analyze_geography("Remote - United States")
        assert REMOTE_US in result.tags
        assert result.us_based is True

    def test_non_us_locations_get_no_us_tag(self) -> None:
        for location in ("London", "Toronto, Canada", "Bengaluru, India", "Berlin"):
            result = analyze_geography(location)
            assert result.tags == (), location
            assert result.us_based is False

    def test_remote_emea_is_not_remote_us(self) -> None:
        result = analyze_geography("Remote - EMEA")
        assert REMOTE_US not in result.tags

    def test_unknown_geography_is_unknown_not_rejected(self) -> None:
        """A bare 'Remote' says nothing about country; do not guess either way."""

        result = analyze_geography("Remote")
        assert result.tags == ()
        assert result.us_based is None
        assert result.remote is True

    def test_a_us_and_non_us_posting_keeps_the_us_tag(self) -> None:
        result = analyze_geography(
            "New York, NY", secondary_locations=["London, United Kingdom"]
        )
        assert NYC in result.tags
        assert result.us_based is True

    def test_tags_are_returned_in_display_order(self) -> None:
        result = analyze_geography(
            "Austin, TX",
            secondary_locations=["Jersey City, NJ", "New York, NY"],
            workplace_type="Remote",
        )
        assert list(result.tags) == [tag for tag in GEO_TAGS if tag in result.tags]

    def test_every_tag_has_a_readable_label(self) -> None:
        assert [label_for(tag) for tag in GEO_TAGS] == [
            "NYC",
            "New Jersey",
            "Remote US",
            "Other US",
        ]
