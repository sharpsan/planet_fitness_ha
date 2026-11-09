"""ZIP code lookup from the bundled Census table."""

from __future__ import annotations

import pytest

from custom_components.planet_fitness.zip_codes import parse_zip, zip_location


@pytest.mark.parametrize(
    ("text", "expected"),
    [("64105", "64105"), (" 64105 ", "64105"), ("64105-1234", "64105"), ("64105 1234", "64105"), ("02134", "02134")],
)
def test_parse_zip(text: str, expected: str) -> None:
    assert parse_zip(text) == expected


FULL_WIDTH_ZIP = "\uff16\uff14\uff11\uff10\uff15"  # 64105 in full-width digits


@pytest.mark.parametrize("text", ["6410", "641055", "64105-12", "abcde", "", "Kansas City", FULL_WIDTH_ZIP])
def test_parse_zip_rejects(text: str) -> None:
    assert parse_zip(text) is None


def test_zip_location() -> None:
    assert zip_location("64105") == (39.11, -94.59)
    assert zip_location("00601") == (18.18, -66.75)  # first row


def test_zip_without_an_area_gets_the_closest_zip_in_its_hub() -> None:
    assert zip_location("64103") == zip_location("64102")  # PO box ZIP between 64102 and 64105


def test_unknown_zip() -> None:
    assert zip_location("00000") is None
