"""Suggest the time range of the available data."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

FACETS = frozenset({"timerange", "start_year", "end_year"})
"""Facets that select the time range of the data."""

# Any time range, so all available data is used.
_ANY = "*"

# The number of digits of the year, the month, and the day at the start of a
# date.
_YEAR_DIGITS = 4
_MONTH_DIGITS = 6
_DAY_DIGITS = 8

# The last day of a month that can be the end of the month, including the
# 30th of each month in a 360-day calendar and the 28th of February in a
# calendar without leap days.
_LAST_DAY = 30
_FEBRUARY = 2
_LAST_DAY_FEBRUARY = 28

# The length of an ISO 8601 time without fractions of a second or time zone.
_ISO_DATETIME_LENGTH = 19


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
        year. ``None`` if the times cannot be read. Time zones are ignored.
    """
    try:
        first, last = (
            dt.datetime.fromisoformat(t[:_ISO_DATETIME_LENGTH])
            for t in (start, end)
        )
    except ValueError:
        return None
    if (
        first
        < last
        == last.replace(month=1, day=1, hour=0, minute=0, second=0)
    ):
        last -= dt.timedelta(seconds=1)
    return f"{first.year:04d}", f"{last.year:04d}"


def _is_month_start(date: str) -> bool:
    """Check if a date is at the start of a month."""
    return len(date) <= _MONTH_DIGITS or date[_MONTH_DIGITS:] == "01"


def _is_month_end(date: str) -> bool:
    """Check if a date is at the end of a month, in any calendar."""
    if len(date) <= _MONTH_DIGITS:
        return True
    month, day = (
        int(date[_YEAR_DIGITS:_MONTH_DIGITS]),
        int(date[_MONTH_DIGITS:]),
    )
    return day >= (_LAST_DAY_FEBRUARY if month == _FEBRUARY else _LAST_DAY)


def _range(start: str, end: str) -> str:
    """Get a time range, in years or months if it covers whole ones."""
    start, end = start[:_DAY_DIGITS], end[:_DAY_DIGITS]
    if not (_is_month_start(start) and _is_month_end(end)):
        return f"{start}/{end}"
    start, end = start[:_MONTH_DIGITS], end[:_MONTH_DIGITS]
    if start[_YEAR_DIGITS:] in ("", "01") and end[_YEAR_DIGITS:] in ("", "12"):
        return f"{_year(start)}/{_year(end)}"
    return f"{start}/{end}"


def join(ranges: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Join the time ranges of the experiments of a dataset.

    Like the check of the data availability of ESMValCore, years without
    data are gaps, so time ranges are only joined if there is no year
    between them, and a time range is suggested for each part.
    """
    joined: list[tuple[str, str]] = []
    for start, end in sorted(ranges):
        if joined and int(start[:4]) <= int(joined[-1][1][:4]) + 1:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


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
        or months are given in years or months, others with the day. Empty
        if there are no ranges.
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


def _dates(value: str) -> tuple[str, str] | None:
    """Get the first and last day of a time range from :func:`suggestions`.

    The last day of a month is that in the standard calendar. Dates that
    cannot be read give ``None``.
    """
    start, slash, end = value.partition("/")
    if not (slash and start.isdigit() and end.isdigit()):
        return None
    if len(start) <= _YEAR_DIGITS:
        start = f"{start.zfill(_YEAR_DIGITS)}01"
    if len(end) <= _YEAR_DIGITS:
        end = f"{end.zfill(_YEAR_DIGITS)}12"
    if len(end) == _MONTH_DIGITS:
        year, month = int(end[:_YEAR_DIGITS]), int(end[_YEAR_DIGITS:])
        try:
            end += f"{calendar.monthrange(year, month)[1]:02d}"
        except ValueError:
            return None
    return f"{start}01"[:_DAY_DIGITS], end[:_DAY_DIGITS]


def overlap(facet: str, suggested: Iterable[Iterable[str]]) -> list[str]:
    """Get the values of a time facet that select data of all datasets.

    Parameters
    ----------
    facet:
        The facet, one of :obj:`FACETS`.
    suggested:
        The values of ``timerange`` from :func:`suggestions` for each
        dataset. Datasets without time ranges are ignored.

    Returns
    -------
    :
        The values of the facet, like :func:`suggestions`, for the parts of
        the time ranges that the datasets have in common.
    """
    common: set[tuple[str, str]] | None = None
    for values in suggested:
        ranges = {dates for v in values if (dates := _dates(v))}
        if not ranges:
            continue
        common = (
            ranges
            if common is None
            else {
                (max(start, other_start), min(end, other_end))
                for start, end in common
                for other_start, other_end in ranges
                if max(start, other_start) <= min(end, other_end)
            }
        )
    return suggestions(facet, common or [])
