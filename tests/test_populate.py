from __future__ import annotations

from concurrent.futures import Future
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest
from lsprotocol import types

from esmvaltool_language_server.populate import (
    SearchError,
    dataset_lists,
    populate_edits,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# The ensemble members of each dataset with data for each variable.
DATA = {
    "tas": {
        "CESM2": ["r1i1p1f1", "r2i1p1f1", "r3i1p1f1"],
        "MIROC6": ["r1i1p1f1"],
        "UKESM1-0-LL": ["r1i1p1f2"],
    },
    "pr": {
        "CESM2": ["r1i1p1f1", "r2i1p1f1", "r3i1p1f1", "r5i1p1f1"],
        "UKESM1-0-LL": ["r1i1p1f2"],
    },
}

RECIPE = dedent("""\
    datasets:
      - {dataset: '*', project: CMIP6, exp: historical, ensemble: 'r*i1p1f1'}
      - dataset: ERA5
        project: OBS6
      -   dataset: 'UK*'
          project: CMIP6
          ensemble: '*'
    diagnostics:
      diag:
        additional_datasets: [{dataset: '*', project: CMIP6}, {dataset: A}]
        variables:
          tas: {mip: Amon}
          pr:
            mip: Amon
            additional_datasets:
              - {dataset: 'M*', project: CMIP6, ensemble: r1i1p1f1}
        scripts: null
    """)


def _search(
    project: str,
    facets: Mapping[object, object],
    facet: str,
) -> Future[list[str]] | None:
    """Find the datasets or ensemble members with data."""
    assert project == "CMIP6"
    datasets = DATA[str(facets["short_name"])]
    future: Future[list[str]] = Future()
    ensemble = facets.get("ensemble")
    if facet == "dataset":
        future.set_result(
            sorted(
                name
                for name, ensembles in datasets.items()
                if ensemble is None
                or "*" in str(ensemble)
                or ensemble in ensembles
            ),
        )
    elif facet == "ensemble":
        future.set_result(datasets.get(str(facets["dataset"]), []))
    else:
        return None
    return future


def _edit(
    start: tuple[int, int],
    end: tuple[int, int],
    new_text: str,
) -> types.TextEdit:
    return types.TextEdit(
        range=types.Range(
            start=types.Position(line=start[0], character=start[1]),
            end=types.Position(line=end[0], character=end[1]),
        ),
        new_text=new_text,
    )


def test_dataset_lists() -> None:
    assert dataset_lists(RECIPE) == [
        (("datasets",), 0),
        (("diagnostics", "diag", "additional_datasets"), 9),
        (
            ("diagnostics", "diag", "variables", "pr", "additional_datasets"),
            14,
        ),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "datasets: [{dataset: CESM2}]\n",
        "datasets: [*a]\n",
        "datasets: [\n",
        "diagnostics: {diag: {variables: {tas: {additional_datasets: 1}}}}\n",
    ],
)
def test_no_dataset_lists(text: str) -> None:
    assert dataset_lists(text) == []


def test_populate_datasets() -> None:
    edits = populate_edits(RECIPE, ("datasets",), _search)
    assert edits == [
        _edit(
            (1, 4),
            (1, 73),
            "{dataset: CESM2, ensemble: 'r(1:3)i1p1f1', exp: historical, "
            "project: CMIP6}",
        ),
        _edit(
            (4, 6),
            (6, 19),
            "{dataset: UKESM1-0-LL, ensemble: r1i1p1f2, project: CMIP6}",
        ),
    ]


def test_populate_diagnostic_datasets() -> None:
    path = ("diagnostics", "diag", "additional_datasets")
    assert populate_edits(RECIPE, path, _search) == [
        _edit(
            (9, 26),
            (9, 56),
            "{dataset: CESM2, project: CMIP6}, "
            "{dataset: UKESM1-0-LL, project: CMIP6}",
        ),
    ]


def test_populate_variable_datasets() -> None:
    path = ("diagnostics", "diag", "variables", "pr", "additional_datasets")
    with pytest.raises(SearchError, match=r"No data found for \{dataset: M"):
        populate_edits(RECIPE, path, _search)


def test_populate_several_items() -> None:
    path = ("diagnostics", "diag", "variables", "tas", "additional_datasets")
    text = RECIPE.replace(
        "tas: {mip: Amon}",
        "tas:\n        mip: Amon\n        additional_datasets:\n"
        "          - {dataset: '*', project: CMIP6, ensemble: r1i1p1f1}",
    )
    edits = populate_edits(text, path, _search)
    assert [edit.new_text for edit in edits] == [
        (
            "{dataset: CESM2, ensemble: r1i1p1f1, project: CMIP6}\n"
            "          - {dataset: MIROC6, ensemble: r1i1p1f1, project: CMIP6}"
        ),
    ]


def test_populate_skips_anchors_and_aliases() -> None:
    text = dedent("""\
        datasets:
          - &cesm {dataset: '*', project: CMIP6}
          - *cesm
        diagnostics:
          diag:
            variables:
              tas: {mip: Amon}
        """)
    assert populate_edits(text, ("datasets",), _search) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "datasets: [{dataset: '*', project: CMIP6}]\n",
            "No variable groups",
        ),
        (
            RECIPE.replace("ensemble: '*'", "grid: '*'"),
            "Cannot search for the values of 'grid'",
        ),
        (
            RECIPE.replace("project: CMIP6", "project: [CMIP6]"),
            "Cannot search for the values of 'dataset'",
        ),
    ],
)
def test_populate_fails(text: str, message: str) -> None:
    with pytest.raises(SearchError, match=message):
        populate_edits(text, ("datasets",), _search)


def test_time_range_patterns_are_kept() -> None:
    # ESMValCore replaces the wildcards in timerange when it finds the data.
    text = dedent("""\
        datasets:
          - {dataset: 'M*', project: CMIP6, timerange: '*'}
          - {dataset: CESM2, project: CMIP6, timerange: '1990/*'}
        diagnostics:
          diag:
            variables:
              tas: {mip: Amon}
        """)

    def search(
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> Future[list[str]] | None:
        if facet == "timerange":
            future: Future[list[str]] = Future()
            future.set_result(["*", "1850/2014", "1990/2014"])
            return future
        return _search(project, facets, facet)

    edits = populate_edits(text, ("datasets",), search)

    assert [edit.new_text for edit in edits] == [
        "{dataset: MIROC6, project: CMIP6, timerange: '*'}",
    ]
