from __future__ import annotations

from concurrent.futures import Future
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest
import yaml
from lsprotocol import types

from esmvaltool_language_server.populate import (
    Progress,
    SearchCancelledError,
    SearchError,
    dataset_lists,
    populate_edits,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

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


# The values of the facets with glob patterns of each dataset.
_Rows = list[tuple[str, ...]]


def _find(
    project: str,
    facets: Mapping[object, object],
    fields: tuple[str, ...],
) -> Future[_Rows] | None:
    """Find the datasets and ensemble members with data."""
    assert project == "CMIP6"
    if not set(fields) <= {"dataset", "ensemble"}:
        return None
    found = [
        {"dataset": name, "ensemble": member}
        for name, members in DATA[str(facets["short_name"])].items()
        for member in members
    ]
    future: Future[_Rows] = Future()
    future.set_result(
        sorted(
            {
                tuple(dataset[f] for f in fields)
                for dataset in found
                if all(
                    facets.get(k, v) == v
                    for k, v in dataset.items()
                    if k not in fields
                )
            },
        ),
    )
    return future


class _Search:
    """Find values with a function, and remember the cancelled searches."""

    def __init__(
        self,
        find: Callable[
            [str, Mapping[object, object], tuple[str, ...]],
            Future[_Rows] | None,
        ] = _find,
        *,
        complete: bool = True,
    ) -> None:
        self.find = find
        self.complete = complete
        self.cancelled: list[Future[_Rows]] = []

    def submit_datasets(
        self,
        project: str,
        facets: Mapping[object, object],
        fields: tuple[str, ...],
    ) -> Future[_Rows] | None:
        return self.find(project, facets, fields)

    def is_complete(self, _: Future[_Rows]) -> bool:
        return self.complete

    def cancel(self, futures: Collection[Future[_Rows]]) -> None:
        self.cancelled.extend(futures)


def _edits(
    text: str,
    path: tuple[str, ...] = ("datasets",),
    search: _Search | None = None,
) -> list[types.TextEdit]:
    return populate_edits(text, path, search or _Search()).edits


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
        # The entry with a glob pattern cannot be replaced.
        "a: &a CMIP6\ndatasets: [{dataset: '*', project: *a}]\n",
    ],
)
def test_no_dataset_lists(text: str) -> None:
    assert dataset_lists(text) == []


def test_populate_datasets() -> None:
    edits = _edits(RECIPE)
    assert edits == [
        _edit(
            (1, 4),
            (1, 73),
            "{dataset: CESM2, project: CMIP6, exp: historical, "
            "ensemble: 'r(1:3)i1p1f1'}",
        ),
        # Block mappings stay block mappings, at the same column.
        _edit(
            (4, 6),
            (6, 19),
            "dataset: UKESM1-0-LL\n      project: CMIP6\n"
            "      ensemble: r1i1p1f2",
        ),
    ]


def test_populate_diagnostic_datasets() -> None:
    path = ("diagnostics", "diag", "additional_datasets")
    assert _edits(RECIPE, path) == [
        _edit(
            (9, 26),
            (9, 56),
            "{dataset: CESM2, project: CMIP6}, "
            "{dataset: UKESM1-0-LL, project: CMIP6}",
        ),
    ]


def test_populate_variable_datasets() -> None:
    path = ("diagnostics", "diag", "variables", "pr", "additional_datasets")
    populated = populate_edits(RECIPE, path, _Search())
    assert populated.edits == []
    assert populated.missing == [
        "{dataset: M*, project: CMIP6, ensemble: r1i1p1f1}",
    ]


def test_populate_keeps_datasets_without_data() -> None:
    text = RECIPE.replace("'UK*'", "'X*'")
    populated = populate_edits(text, ("datasets",), _Search())
    [edit] = populated.edits
    assert edit.range.start.line == 1
    assert populated.missing == [
        "{dataset: X*, project: CMIP6, ensemble: '*'}"
    ]


def test_populate_several_items() -> None:
    path = ("diagnostics", "diag", "variables", "tas", "additional_datasets")
    text = RECIPE.replace(
        "tas: {mip: Amon}",
        "tas:\n        mip: Amon\n        additional_datasets:\n"
        "          - {dataset: '*', project: CMIP6, ensemble: r1i1p1f1}",
    )
    edits = _edits(text, path)
    assert [edit.new_text for edit in edits] == [
        (
            "{dataset: CESM2, project: CMIP6, ensemble: r1i1p1f1}\n"
            "          - {dataset: MIROC6, project: CMIP6, ensemble: r1i1p1f1}"
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
    assert _edits(text) == []


@pytest.mark.parametrize(
    "entry",
    [
        # A merge key.
        "{dataset: '*', <<: *common}",
        "dataset: '*'\n    <<: *common",
        # An alias as the last value, whose node is before the entry.
        "dataset: '*'\n    project: *project",
        "{dataset: '*', project: CMIP6, exp: [*exp]}",
        # An anchor inside the entry.
        "{dataset: '*', project: &other CMIP6}",
        # An anchor after a tag.
        "!!map &a {dataset: '*', project: CMIP6}",
        "{dataset: '*', project: !!str &other CMIP6}",
    ],
)
def test_populate_skips_entries_with_aliases(entry: str) -> None:
    text = dedent("""\
        common: &common {project: CMIP6}
        project: &project CMIP6
        exp: &exp historical
        datasets:
          - ENTRY
        diagnostics:
          diag:
            variables:
              tas: {mip: Amon}
        """).replace("ENTRY", entry)
    assert _edits(text) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "datasets: [{dataset: '*', project: CMIP6}]\n",
            "No variable groups",
        ),
        (
            RECIPE.replace("ensemble: '*'", "grid: '*'"),
            "Cannot search for the values of 'dataset', 'grid'",
        ),
        (
            RECIPE.replace("project: CMIP6", "project: [CMIP6]"),
            "Cannot search for the values of 'dataset'",
        ),
    ],
)
def test_populate_fails(text: str, message: str) -> None:
    with pytest.raises(SearchError, match=message):
        _edits(text)


def test_populate_changed_recipe() -> None:
    # The recipe changed after the editor got the path of the list.
    text = "diagnostics: [diag]\n"
    path = ("diagnostics", "diag", "additional_datasets")
    with pytest.raises(SearchError, match="No variable groups"):
        _edits(text, path)


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

    # The time range is not searched for.
    edits = _edits(text)

    assert [edit.new_text for edit in edits] == [
        "{dataset: MIROC6, project: CMIP6, timerange: '*'}",
    ]


def test_long_datasets_are_not_wrapped() -> None:
    # Wrapping a flow mapping would need the indentation of the next lines.
    facets = ", ".join(f"facet_{i}: value_{i}" for i in range(12))
    text = dedent(f"""\
        diagnostics:
          diag:
            variables:
              tas:
                mip: Amon
                additional_datasets:
                  - {{dataset: 'M*', project: CMIP6, {facets}}}
        """)
    path = ("diagnostics", "diag", "variables", "tas", "additional_datasets")

    (edit,) = _edits(text, path)

    assert "\n" not in edit.new_text
    lines = text.splitlines()
    line = lines[edit.range.start.line]
    lines[edit.range.start.line] = (
        line[: edit.range.start.character] + edit.new_text
    )
    recipe = yaml.safe_load("\n".join(lines))
    datasets = recipe["diagnostics"]["diag"]["variables"]["tas"]
    assert datasets["additional_datasets"] == [
        {
            "dataset": "MIROC6",
            "project": "CMIP6",
            **{f"facet_{i}": f"value_{i}" for i in range(12)},
        },
    ]


def test_populate_with_lists() -> None:
    # Values that are shared by the new datasets are not written as anchors
    # and aliases, which would be defined twice.
    text = dedent("""\
        datasets:
          - {dataset: '*', project: CMIP6, exp: [historical, ssp585]}
          - {dataset: 'M*', project: CMIP6, exp: [historical, ssp585]}
        diagnostics:
          diag:
            variables:
              tas: {mip: Amon}
        """)
    edits = _edits(text)
    assert [edit.new_text for edit in edits] == [
        "{dataset: CESM2, project: CMIP6, exp: [historical, ssp585]}, "
        "{dataset: MIROC6, project: CMIP6, exp: [historical, ssp585]}, "
        "{dataset: UKESM1-0-LL, project: CMIP6, exp: [historical, ssp585]}".replace(
            "}, {", "}\n  - {"
        ),
        "{dataset: MIROC6, project: CMIP6, exp: [historical, ssp585]}",
    ]


def test_populate_flow_list() -> None:
    # The dash in the name of the first dataset does not start an item.
    text = dedent("""\
        datasets: [{dataset: UKESM1-0-LL, project: CMIP6}, {dataset: 'M*',
                    project: CMIP6}]
        diagnostics:
          diag:
            variables:
              tas: {mip: Amon}
        """)
    assert _edits(text) == [
        _edit((0, 51), (1, 27), "{dataset: MIROC6, project: CMIP6}"),
    ]


def test_populate_block_list_on_next_line() -> None:
    text = dedent("""\
        datasets:
        -
            dataset: '*'
            project: CMIP6
            ensemble: r1i1p1f2
        diagnostics:
          diag:
            variables:
              tas: {mip: Amon}
        """)
    assert _edits(text) == [
        _edit(
            (2, 4),
            (4, 22),
            "dataset: UKESM1-0-LL\n    project: CMIP6\n    ensemble: r1i1p1f2",
        ),
    ]


def test_populate_ensemble_range() -> None:
    # Only CESM2 has data for each ensemble member of the range.
    text = RECIPE.replace("'r*i1p1f1'", "'r(1:3)i1p1f1'").replace(
        "exp: historical, ", ""
    )
    assert _edits(text)[0].new_text == (
        "{dataset: CESM2, project: CMIP6, ensemble: 'r(1:3)i1p1f1'}"
    )


@pytest.mark.parametrize(
    ("facets", "expected"),
    [
        # A variable that is derived if needed uses its own data, as found
        # for ERA5, or else the data of the variables that it is derived
        # from, as found for CESM2.
        ("derive: true", ["CESM2", "ERA5"]),
        ("derive: true, force_derivation: true", ["CESM2"]),
        ("derive: false", ["ERA5"]),
    ],
)
def test_populate_derived_variable(facets: str, expected: list[str]) -> None:
    data = {
        "lwcre": {"ERA5"},
        "rlut": {"CESM2", "MIROC6"},
        "rlutcs": {"CESM2"},
    }

    def find(
        project: str,
        facets: Mapping[object, object],
        fields: tuple[str, ...],
    ) -> Future[_Rows] | None:
        assert fields == ("dataset",)
        future: Future[_Rows] = Future()
        future.set_result(
            [(name,) for name in sorted(data[str(facets["short_name"])])],
        )
        return future

    text = dedent(f"""\
        datasets:
          - {{dataset: '*', project: CMIP6}}
        diagnostics:
          diag:
            variables:
              lwcre: {{mip: Amon, {facets}}}
        """)
    [edit] = _edits(text, search=_Search(find))
    assert edit.new_text == "\n  - ".join(
        f"{{dataset: {name}, project: CMIP6}}" for name in expected
    )


def test_populate_incomplete() -> None:
    populated = populate_edits(
        RECIPE,
        ("datasets",),
        _Search(complete=False),
    )
    assert populated.edits
    assert not populated.complete


def test_populate_reports_progress() -> None:
    reported: list[tuple[str, int]] = []
    populated = populate_edits(
        RECIPE,
        ("datasets",),
        _Search(),
        Progress(report=lambda message, p: reported.append((message, p))),
    )
    assert populated.complete
    # Two entries with patterns, searched for two variable groups.
    assert reported == [
        ("Searching for datasets: 0 of 2", 0),
        ("Searching for datasets: 0 of 2", 50),
    ]


def test_populate_cancelled() -> None:
    pending: Future[_Rows] = Future()
    search = _Search(lambda *_: pending)
    cancelled = iter([False, True])
    with pytest.raises(SearchCancelledError):
        populate_edits(
            RECIPE,
            ("datasets",),
            search,
            Progress(cancelled=lambda: next(cancelled)),
        )
    assert search.cancelled == [pending]


def test_populate_unknown_derived_variable() -> None:
    # Variables that ESMValCore cannot derive are searched for themselves.
    text = RECIPE.replace("tas: {mip: Amon}", "tas: {mip: Amon, derive: true}")
    assert _edits(text) == _edits(RECIPE)


def test_populate_ensemble_list_with_range() -> None:
    text = RECIPE.replace("'r*i1p1f1'", "[r1i1p1f1, 'r(2:3)i1p1f1']")
    with pytest.raises(SearchError, match="cannot be combined"):
        _edits(text)


def test_populate_experiments() -> None:
    # The sources find the datasets with data for each experiment of the
    # list, and for any of the activities.
    data = {
        "historical": {"CESM2", "MIROC6"},
        "ssp585": {"CESM2", "UKESM1-0-LL"},
    }
    searched = []

    def find(
        project: str,
        facets: Mapping[object, object],
        fields: tuple[str, ...],
    ) -> Future[_Rows] | None:
        experiments = facets["exp"]
        assert isinstance(experiments, list)
        searched.append((experiments, facets["activity"]))
        future: Future[_Rows] = Future()
        found = set.intersection(*(data[str(e)] for e in experiments))
        future.set_result([(name,) for name in sorted(found)])
        return future

    text = dedent("""\
        datasets:
          - dataset: '*'
            project: CMIP6
            exp: [historical, ssp585]
            activity: [CMIP, ScenarioMIP]
        diagnostics:
          diag:
            variables:
              tas: {mip: Amon}
        """)
    [edit] = _edits(text, search=_Search(find))
    assert edit.new_text == (
        "dataset: CESM2\n    project: CMIP6\n    exp: [historical, ssp585]\n"
        "    activity: [CMIP, ScenarioMIP]"
    )
    activities = ["CMIP", "ScenarioMIP"]
    assert searched == [(["historical", "ssp585"], activities)]


@pytest.mark.parametrize("style", ["|", ">", "|+"])
def test_populate_block_scalar(style: str) -> None:
    text = dedent(f"""\
        datasets:
          - dataset: 'U*'
            project: CMIP6
            ensemble: r1i1p1f2
            comment: {style}
              A comment.

          - {{dataset: ERA5, project: OBS6}}
        diagnostics:
          diag:
            variables:
              tas: {{mip: Amon}}
        """)
    [edit] = _edits(text)
    assert edit.range.end == types.Position(line=5, character=16)
    lines = text.splitlines(keepends=True)
    new_text = (
        "".join(lines[:1]) + "  - " + edit.new_text + "".join(lines[5:])[16:]
    )
    assert yaml.safe_load(new_text)["datasets"][1] == {
        "dataset": "ERA5",
        "project": "OBS6",
    }
