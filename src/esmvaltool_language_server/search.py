"""Find the values of facets of the available data in the background."""

from __future__ import annotations

import dataclasses
import functools
import logging
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Protocol

from esmvalcore.config import CFG

from esmvaltool_language_server import esgf, files

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Hashable, Mapping
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


def _set_exception(future: Future[list[str]], run: Future[None]) -> None:
    """Fail a search if running it failed."""
    if (exc := run.exception()) is not None:
        future.set_exception(exc)


@dataclasses.dataclass
class _Search:
    """A search for the values of a facet."""

    start: float
    future: Future[list[str]]
    run: Callable[[], list[str]]
    interactive: bool
    """Whether the search has been submitted to the pool for completion."""
    claimed: bool = False
    """Whether a worker has started the search."""
    owners: set[BackgroundSearch] = dataclasses.field(default_factory=set)
    """The background searches that wait for the search."""


class FacetSearch:
    """Find the values of facets of the available data in the background.

    The searches can be started from several threads. Searches for
    completion have their own workers, so they do not wait for the many
    searches of :attr:`background`.

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
        # The searches for which a source could not be searched.
        self._incomplete: weakref.WeakSet[Future[list[str]]] = (
            weakref.WeakSet()
        )
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="facet-search",
        )
        self._background_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="background-facet-search",
        )
        self._searches: dict[Hashable, _Search] = {}
        self._lock = threading.Lock()

    def background(self) -> BackgroundSearch:
        """Start searches that may wait for the searches for completion.

        Returns
        -------
        :
            The searches of one user of the facet search, e.g. a search for
            datasets, which can cancel them without cancelling the searches
            of the other users.
        """
        return BackgroundSearch(self)

    def submit(
        self,
        project: str,
        facets: Mapping[object, object],
        facet: str,
        *,
        background: BackgroundSearch | None = None,
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
        background:
            The background search that starts the search, which may wait for
            the searches for completion. None for completion.

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
        with self._lock:
            self._remove_expired()
            search = self._searches.get(key)
            if search is None:
                search = _Search(
                    start=time.monotonic(),
                    future=Future(),
                    run=functools.partial(
                        _search, sources, project, relevant, facet
                    ),
                    interactive=background is None,
                )
                self._searches[key] = search
                executor = (
                    self._executor
                    if background is None
                    else self._background_executor
                )
                self._start(executor, search)
            elif background is None and not search.interactive:
                # Do not wait for the background searches that are before it.
                search.interactive = True
                self._start(self._executor, search)
            if background is not None:
                search.owners.add(background)
            return search.future

    def _start(self, executor: ThreadPoolExecutor, search: _Search) -> None:
        """Run a search with an executor, unless another one runs it first."""
        executor.submit(self._run, search).add_done_callback(
            functools.partial(_set_exception, search.future),
        )

    def _run(self, search: _Search) -> None:
        with self._lock:
            if (
                search.claimed
                or not search.future.set_running_or_notify_cancel()
            ):
                return
            search.claimed = True
        search.future.set_result(self._find(search))

    def is_complete(self, future: Future[list[str]]) -> bool:
        """Check that all sources of the values of a facet could be searched.

        Parameters
        ----------
        future:
            The values, from :meth:`submit`.

        Returns
        -------
        :
            False if a source could not be searched, so values may be missing.
        """
        with self._lock:
            return future not in self._incomplete

    def cancel(
        self,
        background: BackgroundSearch,
        futures: Collection[Future[list[str]]],
    ) -> None:
        """Stop waiting for searches, and cancel those that wait for no one.

        Only the searches that have not started yet and that no search for
        completion or other background search waits for are cancelled.

        Parameters
        ----------
        background:
            The background search that stops waiting.
        futures:
            The values, from :meth:`submit`.
        """
        with self._lock:
            for key, search in list(self._searches.items()):
                if search.future not in futures:
                    continue
                search.owners.discard(background)
                if (
                    not search.owners
                    and not search.interactive
                    and search.future.cancel()
                ):
                    del self._searches[key]

    def _remove_expired(self) -> None:
        """Forget the searches that have finished too long ago.

        Only call this while holding the lock.
        """
        now = time.monotonic()
        for key, search in list(self._searches.items()):
            max_age = (
                self.retry_age
                if search.future in self._incomplete
                else self.max_age
            )
            if now - search.start >= max_age and search.future.done():
                del self._searches[key]

    def _find(self, search: _Search) -> list[str]:
        """Search and remember if a source could not be searched."""
        failures = esgf.failures()
        values = search.run()
        if esgf.failures() != failures:
            with self._lock:
                self._incomplete.add(search.future)
        return values


class BackgroundSearch:
    """Start searches that may wait for the searches for completion.

    Searches that are shared with other background searches are only
    cancelled when none of them waits for them anymore.

    Parameters
    ----------
    search:
        The facet search.
    """

    def __init__(self, search: FacetSearch) -> None:
        self._search = search

    def submit(
        self,
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> Future[list[str]] | None:
        """Start finding the values of a facet."""
        return self._search.submit(project, facets, facet, background=self)

    def is_complete(self, future: Future[list[str]]) -> bool:
        """Check that all sources could be searched."""
        return self._search.is_complete(future)

    def cancel(self, futures: Collection[Future[list[str]]]) -> None:
        """Cancel the searches that have not started yet."""
        self._search.cancel(self, futures)
