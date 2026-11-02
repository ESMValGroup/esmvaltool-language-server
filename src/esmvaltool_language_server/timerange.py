"""Suggest the time range of the available data."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

FACETS = frozenset({"timerange", "start_year", "end_year"})
"""Facets that select the time range of the data."""

# Any time range, so all available data is used.
_ANY = "*"

# The number of digits of the year at the start of a date.
_YEAR_DIGITS = 4


def _year(date: str) -> str | None:
    """Get the year of a date that starts with a four digit year."""
    year = date[:_YEAR_DIGITS]
    if len(year) != _YEAR_DIGITS or not year.isdigit():
        return None
    return str(int(year))


def suggestions(facet: str, ranges: Iterable[tuple[str, str]]) -> list[str]:
    """Get the values of a time facet from the time ranges of datasets.

    Parameters
    ----------
    facet:
        The facet, one of :obj:`FACETS`.
    ranges:
        The first and last date of each dataset, starting with the year.

    Returns
    -------
    :
        The first years for ``start_year``, the last years for ``end_year``,
        and for ``timerange``, the range of years of each dataset and
        ``*``, which selects all available data. Empty if there are no
        ranges.
    """
    years = [
        (start, end)
        for start, end in ((_year(s), _year(e)) for s, e in ranges)
        if start is not None and end is not None
    ]
    if facet == "start_year":
        return sorted({start for start, _ in years})
    if facet == "end_year":
        return sorted({end for _, end in years})
    if not years:
        return []
    return [_ANY, *sorted({f"{start}/{end}" for start, end in years})]
