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

# The number of digits of the year, month, and day at the start of a date.
_YEAR_DIGITS = 4
_MONTH_DIGITS = 6
_DAY_DIGITS = 8

# The number of digits of a date for the frequencies of the data. Dates of
# data with other frequencies, e.g. sub-daily, are given with the day.
_DIGITS = {"dec": _YEAR_DIGITS, "yr": _YEAR_DIGITS, "mon": _MONTH_DIGITS}

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


def _start(date: dt.datetime, digits: int) -> dt.datetime:
    """Get the start of the year, month, or day that contains a date."""
    date = date.replace(hour=0, minute=0, second=0, microsecond=0)
    if digits <= _MONTH_DIGITS:
        date = date.replace(day=1)
    if digits <= _YEAR_DIGITS:
        date = date.replace(month=1)
    return date


def _format(date: dt.datetime, digits: int) -> str:
    return f"{date.year:04d}{date.month:02d}{date.day:02d}"[:digits]


def from_iso(
    start: str,
    end: str,
    frequency: str | None,
) -> tuple[str, str] | None:
    """Get the first and last date of data from ISO 8601 times.

    Parameters
    ----------
    start:
        The time of the first data, or the start of its period.
    end:
        The time of the last data, or the end of its period.
    frequency:
        The frequency of the data, e.g. ``mon``.

    Returns
    -------
    :
        The dates, with the year, month, or day depending on the frequency.
        An end at the start of a year, month, or day is the end of the
        previous one, e.g. ``2101-01-01T00:00:00Z`` is in December 2100 for
        monthly data. ``None`` for data without a time.
    """
    if frequency == "fx":
        return None
    digits = _DIGITS.get(str(frequency), _DAY_DIGITS)
    try:
        first, last = (
            dt.datetime.fromisoformat(time) for time in (start, end)
        )
    except ValueError:
        return None
    if first < last == _start(last, digits):
        last -= dt.timedelta(seconds=1)
    return _format(first, digits), _format(last, digits)


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
