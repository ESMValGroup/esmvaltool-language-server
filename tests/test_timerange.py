from __future__ import annotations

import pytest

from esmvaltool_language_server import timerange

RANGES = [
    ("185001", "201412"),
    ("18500101", "21001230"),
    ("0850", "1849"),
    ("195003", "201411"),
    ("19500101T0030", "20141231T2330"),
    ("unknown", "2014"),
]


@pytest.mark.parametrize(
    ("facet", "expected"),
    [
        (
            "timerange",
            [
                "*",
                "1850/2014",
                "1850/2100",
                "1950/2014",
                "195003/201411",
                "850/1849",
            ],
        ),
        ("start_year", ["1850", "1950", "850"]),
        ("end_year", ["1849", "2014", "2100"]),
    ],
)
def test_suggestions(facet: str, expected: list[str]) -> None:
    assert timerange.suggestions(facet, RANGES) == expected


def test_suggestions_without_ranges() -> None:
    assert timerange.suggestions("timerange", []) == []


@pytest.mark.parametrize(
    ("start", "end", "frequency", "expected"),
    [
        # Time points in the middle of the period.
        (
            "1950-01-16T12:00:00Z",
            "2014-12-16T12:00:00Z",
            "mon",
            ("195001", "201412"),
        ),
        (
            "1950-01-01T12:00:00Z",
            "2014-12-31T12:00:00Z",
            "day",
            ("19500101", "20141231"),
        ),
        (
            "1950-07-02T00:00:00Z",
            "2014-07-02T00:00:00Z",
            "yr",
            ("1950", "2014"),
        ),
        # The start and end of the periods.
        (
            "2022-01-01T00:00:00Z",
            "2101-01-01T00:00:00Z",
            "mon",
            ("202201", "210012"),
        ),
        (
            "2022-01-01T00:00:00Z",
            "2101-01-01T00:00:00Z",
            "day",
            ("20220101", "21001231"),
        ),
        (
            "2022-01-01T00:00:00Z",
            "2101-01-01T00:00:00Z",
            "yr",
            ("2022", "2100"),
        ),
        # Sub-daily data is given with the day.
        (
            "1950-01-01T00:30:00Z",
            "2014-12-31T23:30:00Z",
            "1hr",
            ("19500101", "20141231"),
        ),
        (
            "1950-01-01T00:00:00Z",
            "2014-12-31T18:00:00Z",
            "6hr",
            ("19500101", "20141231"),
        ),
        # Unknown frequencies are given with the day.
        (
            "1950-01-16T12:00:00Z",
            "2014-12-16T12:00:00Z",
            None,
            ("19500116", "20141216"),
        ),
        # A single time.
        (
            "1850-01-01T00:00:00Z",
            "1850-01-01T00:00:00Z",
            "mon",
            ("185001", "185001"),
        ),
        # No time.
        ("1850-01-01T00:00:00Z", "1850-01-01T00:00:01Z", "fx", None),
        ("unknown", "2014-12-16T12:00:00Z", "mon", None),
    ],
)
def test_from_iso(
    start: str,
    end: str,
    frequency: str | None,
    expected: tuple[str, str] | None,
) -> None:
    assert timerange.from_iso(start, end, frequency) == expected
