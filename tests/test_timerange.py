from __future__ import annotations

import pytest

from esmvaltool_language_server import timerange

RANGES = [
    ("185001", "201412"),
    ("1850-01-15T12:00:00Z", "2100-12-15T12:00:00Z"),
    ("0850", "1849"),
    ("unknown", "2014"),
]


@pytest.mark.parametrize(
    ("facet", "expected"),
    [
        ("timerange", ["*", "1850/2014", "1850/2100", "850/1849"]),
        ("start_year", ["1850", "850"]),
        ("end_year", ["1849", "2014", "2100"]),
    ],
)
def test_suggestions(facet: str, expected: list[str]) -> None:
    assert timerange.suggestions(facet, RANGES) == expected


def test_suggestions_without_ranges() -> None:
    assert timerange.suggestions("timerange", []) == []
