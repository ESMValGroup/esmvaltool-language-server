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


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("1850", "2014", "1850/2014"),
        ("185001", "201412", "1850/2014"),
        ("18500101", "20141231", "1850/2014"),
        # A 360-day calendar.
        ("18500101", "20141230", "1850/2014"),
        ("185001010030", "201412312330", "1850/2014"),
        ("185003", "201411", "185003/201411"),
        ("19910601", "19921230", "199106/199212"),
        ("19910601", "19920430", "199106/199204"),
        ("19500101", "19500228", "195001/195002"),
        ("19500101", "19520229", "195001/195202"),
        ("19500115", "20141231", "19500115/20141231"),
        ("19500101", "19500227", "19500101/19500227"),
        ("19500101", "19501229", "19500101/19501229"),
    ],
)
def test_suggestions_precision(start: str, end: str, expected: str) -> None:
    assert timerange.suggestions("timerange", [(start, end)]) == [
        "*",
        expected,
    ]


def test_suggestions_without_ranges() -> None:
    assert timerange.suggestions("timerange", []) == []


@pytest.mark.parametrize(
    ("facet", "suggested", "expected"),
    [
        (
            "timerange",
            [["*", "1850/2014"], ["*", "1850/2021"]],
            ["*", "1850/2014"],
        ),
        (
            "timerange",
            [["*", "850/1850", "195003/201411"], ["*", "1850/2014"]],
            ["*", "1850/1850", "195003/201411"],
        ),
        (
            "timerange",
            [["*", "19500115/20141231"], ["*", "195001/195006"]],
            ["*", "19500115/19500630"],
        ),
        # Datasets without time ranges are ignored.
        ("timerange", [["*", "1850/2014"], []], ["*", "1850/2014"]),
        ("timerange", [["*", "1850/1900"], ["*", "1950/2014"]], []),
        ("timerange", [], []),
        # Dates that cannot be read are ignored.
        ("timerange", [["185001/185000"], ["1850/2014"]], ["*", "1850/2014"]),
        ("start_year", [["*", "1850/2014"], ["*", "1900/2021"]], ["1900"]),
        ("end_year", [["*", "1850/2014"], ["*", "1900/2021"]], ["2014"]),
    ],
)
def test_overlap(
    facet: str,
    suggested: list[list[str]],
    expected: list[str],
) -> None:
    assert timerange.overlap(facet, suggested) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("1850-01-16T12:00:00Z", "2014-12-16T12:00:00Z", ("1850", "2014")),
        ("0850-01-16T12:00:00Z", "1849-12-16T12:00:00Z", ("0850", "1849")),
        # The end of the period of the last data.
        ("2022-01-01T00:00:00Z", "2101-01-01T00:00:00Z", ("2022", "2100")),
        ("2022-01-01T00:00:00Z", "2101-01-01T00:00:01Z", ("2022", "2101")),
        # A single time.
        ("1850-01-01T00:00:00Z", "1850-01-01T00:00:00Z", ("1850", "1850")),
        ("unknown", "2014-12-16T12:00:00Z", None),
        # Time zones and fractions of a second are ignored.
        ("1850-01-16T12:00:00Z", "2015-01-01T00:00:00", ("1850", "2014")),
        ("1850-01-16T12:00:00.000Z", "2014-12-16", ("1850", "2014")),
    ],
)
def test_from_iso(
    start: str,
    end: str,
    expected: tuple[str, str] | None,
) -> None:
    assert timerange.from_iso(start, end) == expected


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        ([], []),
        ([("1850", "2014"), ("2015", "2100")], [("1850", "2100")]),
        ([("185001", "201412"), ("201406", "210012")], [("185001", "210012")]),
        ([("1850", "2100"), ("1900", "1950")], [("1850", "2100")]),
        (
            [("2016", "2100"), ("1850", "2014")],
            [("1850", "2014"), ("2016", "2100")],
        ),
    ],
)
def test_join(
    ranges: list[tuple[str, str]],
    expected: list[tuple[str, str]],
) -> None:
    assert timerange.join(ranges) == expected
