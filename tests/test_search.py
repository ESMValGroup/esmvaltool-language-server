from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from esmvaltool_language_server import esgf, search

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

TIMEOUT = 10


class _Source:
    """Find the given values and remember the searches."""

    def __init__(self, values: list[str], facets: set[str]) -> None:
        self.values = values
        self.facets = facets
        self.searches: list[tuple[str, dict[object, object], str]] = []

    def known_facets(self, _: str) -> set[str]:
        return self.facets

    def is_searchable(self, project: str, facet: str) -> bool:
        return project == "CMIP6" and facet in self.facets

    def facet_values(
        self,
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> list[str]:
        self.searches.append((project, dict(facets), facet))
        if not self.values:
            raise RuntimeError
        return self.values


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> _Source:
    source = _Source(["A"], {"dataset", "exp"})
    monkeypatch.setattr(search, "SOURCES", (source,))
    return source


def test_facet_search(source: _Source) -> None:
    facet_search = search.FacetSearch()
    facets = {"exp": "historical", "preprocessor": "pp", "dataset": "B"}
    future = facet_search.submit("CMIP6", facets, "dataset")
    assert future is not None
    assert future.result() == ["A"]
    # Facets that are not used in the search do not start a new search.
    other = facet_search.submit(
        "CMIP6",
        {**facets, "preprocessor": "x"},
        "dataset",
    )
    assert other is future
    new = facet_search.submit("CMIP6", {**facets, "exp": "ssp585"}, "dataset")
    assert new is not None
    new.result()
    assert source.searches == [
        ("CMIP6", {"exp": "historical"}, "dataset"),
        ("CMIP6", {"exp": "ssp585"}, "dataset"),
    ]
    assert facet_search.submit("CMIP6", facets, "preprocessor") is None
    assert facet_search.submit("OBS6", facets, "dataset") is None


def test_facet_search_expires(source: _Source) -> None:
    facet_search = search.FacetSearch(max_age=0)
    first = facet_search.submit("CMIP6", {}, "dataset")
    assert first is not None
    first.result()
    second = facet_search.submit("CMIP6", {}, "dataset")
    assert second is not None
    assert second is not first
    second.result()
    assert len(source.searches) == 2


def test_facet_search_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    esgf = _Source(["B", "A"], {"dataset", "exp"})
    files = _Source(["C", "A"], {"dataset", "tier"})
    other = _Source(["D"], {"exp"})
    monkeypatch.setattr(search, "SOURCES", (esgf, files, other))
    facets = {"exp": "historical", "tier": 3, "mip": "Amon"}
    future = search.FacetSearch().submit("CMIP6", facets, "dataset")
    assert future is not None
    assert future.result() == ["A", "B", "C"]
    assert other.searches == []
    assert esgf.searches == files.searches == [("CMIP6", facets, "dataset")]


def test_facet_search_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sources = (_Source([], {"dataset"}), _Source(["A"], {"dataset"}))
    monkeypatch.setattr(search, "SOURCES", sources)
    with caplog.at_level(logging.ERROR):
        future = search.FacetSearch().submit("CMIP6", {}, "dataset")
        assert future is not None
        assert future.result() == ["A"]
    assert "Failed to get the values of dataset" in caplog.text


def test_unused_facets(monkeypatch: pytest.MonkeyPatch) -> None:
    esgf = _Source(["A"], {"dataset", "exp"})
    files = _Source(["A"], {"dataset", "tier"})

    def known_facets(project: str) -> set[str]:
        return {"CMIP6": {"dataset", "exp"}, "OBS": {"dataset", "tier"}}.get(
            project,
            set(),
        )

    monkeypatch.setattr(esgf, "known_facets", known_facets)
    monkeypatch.setattr(files, "known_facets", lambda _: set())
    monkeypatch.setattr(search, "SOURCES", (esgf, files))
    projects = {"CMIP6": {}, "OBS": {}, "unknown": {}}
    monkeypatch.setitem(search.CFG, "projects", projects)
    search._all_facets.cache_clear()
    assert search.unused_facets("OBS") == {"exp"}
    assert search.unused_facets("CMIP6") == {"tier"}
    assert search.unused_facets("unknown") == set()
    search._all_facets.cache_clear()


def test_facet_search_retries_after_error(source: _Source) -> None:
    source.values = []
    facet_search = search.FacetSearch(retry_age=0)
    first = facet_search.submit("CMIP6", {}, "dataset")
    assert first is not None
    assert first.result() == []
    source.values = ["A"]
    second = facet_search.submit("CMIP6", {}, "dataset")
    assert second is not None
    assert second is not first
    assert second.result() == ["A"]
    # The second search found all values, so it is remembered.
    assert facet_search.submit("CMIP6", {}, "dataset") is second
    assert len(source.searches) == 2


def test_facet_search_retries_after_failure_of_index(
    monkeypatch: pytest.MonkeyPatch,
    source: _Source,
) -> None:
    def facet_values(*_: object) -> list[str]:
        # An index failed, but the others found values.
        esgf.record_failure("index")
        return ["A"]

    monkeypatch.setattr(source, "facet_values", facet_values)
    facet_search = search.FacetSearch(retry_age=0)
    first = facet_search.submit("CMIP6", {}, "dataset")
    assert first is not None
    assert first.result() == ["A"]
    assert facet_search.submit("CMIP6", {}, "dataset") is not first


def test_facet_search_remembers_failure_until_retry(source: _Source) -> None:
    source.values = []
    facet_search = search.FacetSearch()
    first = facet_search.submit("CMIP6", {}, "dataset")
    assert first is not None
    first.result()
    assert facet_search.submit("CMIP6", {}, "dataset") is first


def test_facet_search_is_complete(source: _Source) -> None:
    facet_search = search.FacetSearch()
    complete = facet_search.submit("CMIP6", {}, "dataset")
    assert complete is not None
    complete.result()
    assert facet_search.is_complete(complete)
    source.values = []
    incomplete = facet_search.submit("CMIP6", {"exp": "historical"}, "dataset")
    assert incomplete is not None
    incomplete.result()
    assert not facet_search.is_complete(incomplete)


def test_facet_search_cancel(
    monkeypatch: pytest.MonkeyPatch,
    source: _Source,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def facet_values(*_: object) -> list[str]:
        started.set()
        release.wait(TIMEOUT)
        return ["A"]

    monkeypatch.setattr(source, "facet_values", facet_values)
    facet_search = search.FacetSearch()
    facet_search._background_executor = ThreadPoolExecutor(max_workers=1)
    background = facet_search.background()
    running = background.submit("CMIP6", {"exp": "a"}, "dataset")
    assert started.wait(TIMEOUT)
    queued = background.submit("CMIP6", {"exp": "b"}, "dataset")
    assert running is not None
    assert queued is not None
    background.cancel([running, queued])
    release.set()
    # Searches that have started are finished, so they can be used later.
    assert running.result() == ["A"]
    assert facet_search.submit("CMIP6", {"exp": "a"}, "dataset") is running
    assert queued.cancelled()
    again = facet_search.submit("CMIP6", {"exp": "b"}, "dataset")
    assert again is not queued
    assert again is not None
    assert again.result() == ["A"]


def test_facet_search_for_completion_does_not_wait(
    monkeypatch: pytest.MonkeyPatch,
    source: _Source,
) -> None:
    release = threading.Event()

    def facet_values(
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> list[str]:
        if facets.get("exp") == "a":
            release.wait(TIMEOUT)
        return ["A"]

    monkeypatch.setattr(source, "facet_values", facet_values)
    facet_search = search.FacetSearch()
    facet_search._background_executor = ThreadPoolExecutor(max_workers=1)
    background = facet_search.background()
    busy = background.submit("CMIP6", {"exp": "a"}, "dataset")
    queued = background.submit("CMIP6", {"exp": "b"}, "dataset")
    # Completion needs the queued search, so it is run for completion.
    found = facet_search.submit("CMIP6", {"exp": "b"}, "dataset")
    assert found is queued
    assert found is not None
    assert found.result(TIMEOUT) == ["A"]
    assert busy is not None
    assert not busy.done()
    release.set()
    assert busy.result(TIMEOUT) == ["A"]
    assert background.is_complete(busy)
    background.cancel([busy])
    assert facet_search.submit("CMIP6", {"exp": "a"}, "dataset") is busy


@pytest.fixture
def blocked(
    monkeypatch: pytest.MonkeyPatch,
    source: _Source,
) -> Iterator[tuple[search.FacetSearch, Future[list[str]]]]:
    """Get a facet search whose background searches wait for a busy one."""
    release = threading.Event()

    def facet_values(
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> list[str]:
        if facets.get("exp") == "busy":
            release.wait(TIMEOUT)
        return ["A"]

    monkeypatch.setattr(source, "facet_values", facet_values)
    facet_search = search.FacetSearch()
    facet_search._background_executor = ThreadPoolExecutor(max_workers=1)
    busy = facet_search.background().submit(
        "CMIP6", {"exp": "busy"}, "dataset"
    )
    assert busy is not None
    yield facet_search, busy
    release.set()


def test_background_searches_share_searches(
    blocked: tuple[search.FacetSearch, Future[list[str]]],
) -> None:
    facet_search, busy = blocked
    first = facet_search.background()
    second = facet_search.background()
    queued = first.submit("CMIP6", {"exp": "a"}, "dataset")
    assert queued is not None
    assert second.submit("CMIP6", {"exp": "a"}, "dataset") is queued
    # The other background search still waits for the search.
    first.cancel([queued])
    assert not queued.cancelled()
    second.cancel([queued])
    assert queued.cancelled()
    assert not busy.cancelled()


def test_background_search_does_not_cancel_completion(
    blocked: tuple[search.FacetSearch, Future[list[str]]],
) -> None:
    facet_search, _ = blocked
    background = facet_search.background()
    queued = background.submit("CMIP6", {"exp": "a"}, "dataset")
    assert queued is not None
    assert facet_search.submit("CMIP6", {"exp": "a"}, "dataset") is queued
    background.cancel([queued])
    assert queued.result(TIMEOUT) == ["A"]


def test_facet_search_is_incomplete_after_expiry(source: _Source) -> None:
    source.values = []
    facet_search = search.FacetSearch(retry_age=0)
    future = facet_search.submit("CMIP6", {}, "dataset")
    assert future is not None
    future.result(TIMEOUT)
    assert facet_search.submit("CMIP6", {}, "dataset") is not future
    assert not facet_search.is_complete(future)


def test_facet_search_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    facet_search = search.FacetSearch()

    def find(*_: object) -> list[str]:
        raise ValueError

    monkeypatch.setattr(facet_search, "_find", find)
    monkeypatch.setattr(search, "SOURCES", (_Source(["A"], {"dataset"}),))
    future = facet_search.submit("CMIP6", {}, "dataset")
    assert future is not None
    assert isinstance(future.exception(TIMEOUT), ValueError)
