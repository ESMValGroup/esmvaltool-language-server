from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from esmvaltool_language_server import search

if TYPE_CHECKING:
    from collections.abc import Mapping


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
