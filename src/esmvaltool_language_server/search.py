"""Find the values of facets of the available data in the background."""

from __future__ import annotations

import functools
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Protocol

from esmvalcore.config import CFG

from esmvaltool_language_server import esgf, files

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping
    from collections.abc import Set as AbstractSet

logger = logging.getLogger(__name__)


class _Source(Protocol):
    """A module that finds the values of facets."""

    def known_facets(self, project: str) -> AbstractSet[str]: ...

    def is_searchable(self, project: str, facet: str) -> bool: ...

    def facet_values(
        self,
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> list[str]: ...


# The values on ESGF and in the other data sources of ESMValCore.
SOURCES: tuple[_Source, ...] = (esgf, files)


def _facets(project: str) -> set[str]:
    """Get the facets that are used to find the data of a project."""
    return set().union(*(s.known_facets(project) for s in SOURCES))


@functools.cache
def _all_facets() -> frozenset[str]:
    """Get the facets that are used to find the data of any project."""
    return frozenset().union(*(_facets(p) for p in CFG["projects"]))


def unused_facets(project: str) -> set[str]:
    """Get the facets of other projects that are not used by a project.

    Parameters
    ----------
    project:
        The project.

    Returns
    -------
    :
        The facets. Empty if it is not known which facets the project uses.
    """
    used = _facets(project)
    return set(_all_facets() - used) if used else set()


def _search(
    sources: list[_Source],
    project: str,
    facets: Mapping[object, object],
    facet: str,
) -> list[str]:
    def search(source: _Source) -> list[str]:
        try:
            return source.facet_values(project, facets, facet)
        except Exception:
            logger.exception(
                "Failed to get the values of %s from %s",
                facet,
                getattr(source, "__name__", source),
            )
            esgf.record_failure(source)
            return []

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        return sorted(set().union(*pool.map(search, sources)))


class FacetSearch:
    """Find the values of facets of the available data in the background.

    Parameters
    ----------
    max_age:
        Search again after this many seconds.
    retry_age:
        Search again after this many seconds if a source of the values could
        not be searched.
    """

    def __init__(
        self,
        max_age: float = 2 * 60 * 60,
        retry_age: float = 10 * 60,
    ) -> None:
        self.max_age = max_age
        self.retry_age = retry_age
        self._incomplete: set[Hashable] = set()
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._searches: dict[Hashable, tuple[float, Future[list[str]]]] = {}

    def submit(
        self,
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> Future[list[str]] | None:
        """Start finding the values of a facet, unless it has been started.

        Parameters
        ----------
        project:
            The project of the datasets.
        facets:
            The facets of the datasets.
        facet:
            The facet to get the values of.

        Returns
        -------
        :
            The values, sorted. None if they cannot be found.
        """
        sources = [s for s in SOURCES if s.is_searchable(project, facet)]
        if not sources:
            return None
        # Only search again if the facets that are used in the search change.
        known = set().union(
            {"short_name", "mip"},
            *(s.known_facets(project) for s in sources),
        )
        known -= {facet, "project"}
        relevant = {k: v for k, v in facets.items() if k in known}
        key = (project, facet, repr(sorted(relevant.items(), key=str)))
        self._remove_expired()
        if key not in self._searches:
            future = self._executor.submit(
                self._find,
                key,
                sources,
                project,
                relevant,
                facet,
            )
            self._searches[key] = (time.monotonic(), future)
        return self._searches[key][1]

    def _remove_expired(self) -> None:
        """Forget the searches that have finished too long ago."""
        now = time.monotonic()
        for key, (start, future) in list(self._searches.items()):
            max_age = (
                self.retry_age if key in self._incomplete else self.max_age
            )
            if now - start >= max_age and future.done():
                del self._searches[key]
                self._incomplete.discard(key)

    def _find(
        self,
        key: Hashable,
        sources: list[_Source],
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> list[str]:
        """Search and remember if a source could not be searched."""
        failures = esgf.failures()
        values = _search(sources, project, facets, facet)
        if esgf.failures() != failures:
            self._incomplete.add(key)
        return values
