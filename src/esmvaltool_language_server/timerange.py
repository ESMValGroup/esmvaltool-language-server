"""Suggest the time range of the available data."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

FACETS = frozenset({"timerange", "start_year", "end_year"})
"""Facets that select the time range of the data."""

# Any time range, so all available data is used.
_ANY = "*"

# The number of digits of the year and the day at the start of a date.
_YEAR_DIGITS = 4
_DAY_DIGITS = 8

# The month and day after the year of the first and last date of a time range
# that covers whole years, including those in a 360-day calendar.
_FIRST = frozenset({"", "01", "0101"})
_LAST = frozenset({"", "12", "1230", "1231"})


def _year(date: str) -> str | None:
    """Get the year of a date that starts with a four digit year."""
    year = date[:_YEAR_DIGITS]
    if len(year) != _YEAR_DIGITS or not year.isdigit():
        return None
    return str(int(year))


def from_iso(start: str, end: str) -> tuple[str, str] | None:
    """Get the years of the first and last data from ISO 8601 times.

    Parameters
    ----------
    start:
        The time of the first data, or the start of its period.
    end:
        The time of the last data, or the end of its period.

    Returns
    -------
    :
        The years. An end at the start of a year is the end of the previous
        year. ``None`` if the times cannot be read.
    """
    try:
        first, last = (dt.datetime.fromisoformat(t) for t in (start, end))
    except ValueError:
        return None
    if first < last == dt.datetime(last.year, 1, 1, tzinfo=last.tzinfo):
        last -= dt.timedelta(seconds=1)
    return f"{first.year:04d}", f"{last.year:04d}"


def _range(start: str, end: str) -> str:
    """Get a time range, in years if it covers whole years."""
    start, end = start[:_DAY_DIGITS], end[:_DAY_DIGITS]
    if start[_YEAR_DIGITS:] in _FIRST and end[_YEAR_DIGITS:] in _LAST:
        return f"{_year(start)}/{_year(end)}"
    return f"{start}/{end}"


def suggestions(facet: str, ranges: Iterable[tuple[str, str]]) -> list[str]:
    """Get the values of a time facet from the time ranges of datasets.

    Parameters
    ----------
    facet:
        The facet, one of :obj:`FACETS`.
    ranges:
        The first and last date of each dataset, in the format of the dates
        in file names, e.g. ``185001``.

    Returns
    -------
    :
        The first years for ``start_year``, the last years for ``end_year``,
        and for ``timerange``, the time range of each dataset and ``*``,
        which selects all available data. Time ranges that cover whole years
        are given in years, others with the month or day. Empty if there are
        no ranges.
    """
    ranges = [
        (start, end) for start, end in ranges if _year(start) and _year(end)
    ]
    if facet == "start_year":
        return sorted({str(_year(start)) for start, _ in ranges})
    if facet == "end_year":
        return sorted({str(_year(end)) for _, end in ranges})
    if not ranges:
        return []
    return [_ANY, *sorted({_range(start, end) for start, end in ranges})]
