"""Replace datasets with glob patterns by the datasets that have data.

For example, ``{dataset: '*', project: CMIP6, exp: historical, ensemble:
r1i1p1f1}`` in a list of datasets is replaced by the datasets of CMIP6 that
have data for every variable group that uses the list.
The values of the facets with glob patterns are found with the facet search
of :mod:`esmvaltool_language_server.search`, and ensemble members and
sub-experiments are written as ranges by
:func:`esmvalcore.dataset.datasets_to_recipe`.
The new datasets are written in the style of the dataset that they replace.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from concurrent.futures import FIRST_COMPLETED, Future, wait
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Protocol, cast, override

import yaml
from esmvalcore.dataset import Dataset, datasets_to_recipe
from esmvalcore.exceptions import RecipeError
from esmvalcore.preprocessor._derive import get_required
from lsprotocol import types

from esmvaltool_language_server import timerange
from esmvaltool_language_server.schema import data_at

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping

    from esmvalcore.typing import FacetValue

    from esmvaltool_language_server.context import Path


class Search(Protocol):
    """Find the values of facets.

    Refer to :class:`esmvaltool_language_server.search.FacetSearch`.
    """

    def submit(
        self,
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> Future[list[str]] | None:
        """Start finding the values of a facet of a project."""

    def is_complete(self, future: Future[list[str]]) -> bool:
        """Check that all sources of the values could be searched."""

    def cancel(self, futures: Collection[Future[list[str]]]) -> None:
        """Cancel the searches that have not started yet."""


def _no_report(message: str, percentage: int) -> None:
    """Do not report the progress."""


def _not_cancelled() -> bool:
    return False


@dataclasses.dataclass(frozen=True)
class Progress:
    """Report the progress of the search and find out if it was cancelled."""

    report: Callable[[str, int], None] = _no_report
    """Report a message and the percentage of the search that is done."""
    cancelled: Callable[[], bool] = _not_cancelled
    """Check if the user cancelled the search."""


@dataclasses.dataclass(frozen=True)
class Populated:
    """The edits that populate a list of datasets."""

    edits: list[types.TextEdit]
    """The edits, with positions that count characters."""
    complete: bool
    """Whether all sources of data could be searched, so no dataset is
    missing."""


# Facets that :func:`esmvalcore.dataset.datasets_to_recipe` writes as ranges.
# They have the most values, so they are searched last to search less often.
_RANGES = ("ensemble", "sub_experiment")

# A glob pattern, as in :mod:`esmvalcore.dataset`.
_PATTERN = re.compile(r".*[\*\?]+.*|.*\[.*\].*")


def _is_pattern(value: object) -> bool:
    return isinstance(value, str) and bool(_PATTERN.match(value))


def _patterns(entry: dict[object, object]) -> list[tuple[object, object]]:
    """Get the patterns to replace: ESMValCore resolves time range patterns."""
    return [
        (facet, value)
        for facet, value in entry.items()
        if facet not in timerange.FACETS and _is_pattern(value)
    ]


def _has_patterns(entry: object) -> bool:
    return isinstance(entry, dict) and bool(_patterns(entry))


def _list_paths(recipe: object) -> list[Path]:
    """Get the paths of the lists of datasets in a recipe."""
    paths: list[Path] = [("datasets",)]
    diagnostics = data_at(recipe, ("diagnostics",))
    for diagnostic in diagnostics if isinstance(diagnostics, dict) else {}:
        path = ("diagnostics", diagnostic)
        paths.append((*path, "additional_datasets"))
        variables = data_at(recipe, (*path, "variables"))
        paths.extend(
            (*path, "variables", group, "additional_datasets")
            for group in (variables if isinstance(variables, dict) else {})
        )
    return paths


def dataset_lists(text: str) -> list[tuple[Path, int]]:
    """Find the lists of datasets with glob patterns in a recipe.

    Parameters
    ----------
    text:
        The YAML text of the recipe.

    Returns
    -------
    :
        The path of each list and the line of its key. Empty if the recipe
        is not valid YAML.
    """
    try:
        recipe = yaml.safe_load(text)
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return []
    result = []
    for path in _list_paths(recipe):
        entries = data_at(recipe, path)
        nodes = _nodes(root, path)
        if (
            isinstance(entries, list)
            and nodes is not None
            and _replaceable(text, entries, nodes[1])
        ):
            result.append((path, nodes[0].start_mark.line))
    return result


def _replaceable(
    text: str,
    entries: list[object],
    sequence: yaml.Node,
) -> list[tuple[dict[object, object], yaml.Node]]:
    """Get the entries with glob patterns of a list that can be replaced."""
    return [
        (cast("dict[object, object]", entry), node)
        for entry, node in zip(entries, sequence.value, strict=True)
        if _has_patterns(entry) and _is_plain(text, sequence, node)
    ]


def _nodes(
    node: yaml.Node | None,
    path: Path,
) -> tuple[yaml.Node, yaml.Node] | None:
    """Find the key and value nodes at a path of mapping keys."""
    key = None
    for part in path:
        if not isinstance(node, yaml.MappingNode):
            return None
        key, node = next(
            ((k, v) for k, v in node.value if k.value == part),
            (None, None),
        )
    return None if key is None or node is None else (key, node)


def _groups(recipe: object, path: Path) -> list[dict[object, object]]:
    """Get the facets of the variable groups that use a list of datasets."""
    groups = []
    diagnostics = data_at(recipe, ("diagnostics",))
    for diagnostic in diagnostics if isinstance(diagnostics, dict) else {}:
        variables = data_at(recipe, ("diagnostics", diagnostic, "variables"))
        for group in variables if isinstance(variables, dict) else {}:
            group_path = ("diagnostics", diagnostic, "variables", group)
            if group_path[: len(path) - 1] == path[:-1]:
                facets = data_at(recipe, group_path)
                facets = facets if isinstance(facets, dict) else {}
                groups.append({"short_name": group, **facets})
    return groups


class SearchError(Exception):
    """The datasets cannot be found."""


class SearchCancelledError(Exception):
    """The user cancelled the search."""


def _submit(
    search: Search,
    facets: dict[object, object],
    facet: str,
) -> Future[list[str]]:
    """Start finding the values of a facet."""
    project = facets.get("project")
    future = (
        None
        if not isinstance(project, str)
        else search.submit(project, facets, facet)
    )
    if future is None:
        msg = f"Cannot search for the values of '{facet}'."
        raise SearchError(msg)
    return future


def _alternatives(
    facets: dict[object, object],
) -> list[list[dict[object, object]]]:
    """Get the alternative variables whose data a variable group can use.

    As in ESMValCore, a variable with ``derive: true`` uses its own data if
    there is any, and otherwise the data of all the variables that it is
    derived from. With ``force_derivation: true``, it is always derived.
    """
    project = facets.get("project")
    if not facets.get("derive") or not isinstance(project, str):
        return [[facets]]
    try:
        required = get_required(str(facets.get("short_name")), project)
    except NotImplementedError:
        return [[facets]]
    inputs = [
        {**facets, **{k: v for k, v in r.items() if k != "optional"}}
        for r in required
        if not r.get("optional")
    ]
    return [inputs] if facets.get("force_derivation") else [[facets], inputs]


def _needs(
    group: dict[object, object],
    dataset: dict[object, object],
) -> list[list[list[dict[object, object]]]]:
    """Get the data that a dataset needs for a variable group.

    The data of each ensemble member and sub-experiment of a range is needed,
    from one of the alternative variables of :func:`_alternatives`.
    """
    facets = {**group, **dataset}
    try:
        members = Dataset(
            **{str(k): cast("FacetValue", v) for k, v in facets.items()},
        ).from_ranges()
    except RecipeError as exc:
        raise SearchError(str(exc)) from exc
    return [_alternatives({**facets, **m.facets}) for m in members]


@dataclasses.dataclass
class _Searcher:
    """Wait for the searches, report the progress, and stop if cancelled."""

    search: Search
    progress: Progress
    steps: int
    """The number of facets to search for."""
    step: int = 0
    complete: bool = True

    def wait_for(
        self,
        facet: object,
        futures: Collection[Future[list[str]]],
    ) -> None:
        """Wait for the searches for the values of a facet."""
        pending = set(futures)
        total = len(pending)
        while pending:
            if self.progress.cancelled():
                self.search.cancel(pending)
                raise SearchCancelledError
            self.progress.report(
                f"Searching for '{facet}': {total - len(pending)} of {total}",
                int(100 * (self.step + 1 - len(pending) / total) / self.steps),
            )
            _, pending = wait(
                pending, timeout=0.2, return_when=FIRST_COMPLETED
            )
        self.step += 1
        self.complete &= all(map(self.search.is_complete, futures))


# The searches for the data that a dataset needs: all of the outer lists,
# from any of the alternatives, all searches of an alternative.
_Futures = list[list[list[Future[list[str]]]]]


def _found(futures: _Futures) -> list[str]:
    """Get the values with all the data that a dataset needs."""
    found = [
        [[set(f.result()) for f in alt] for alt in need] for need in futures
    ]
    values = set().union(*(v for need in found for alt in need for v in alt))
    return sorted(
        value
        for value in values
        if all(any(all(value in v for v in alt) for alt in n) for n in found)
    )


def _expand(
    searcher: _Searcher,
    groups: list[dict[object, object]],
    entry: dict[object, object],
) -> list[dict[object, object]]:
    """Replace the glob patterns of a dataset by the values with data.

    The values of one facet are searched at a time, and only the values with
    all the data that the datasets need for every group are kept.
    """
    datasets = [entry]
    patterns = sorted(_patterns(entry), key=lambda p: p[0] in _RANGES)
    for facet, pattern in patterns:
        futures: list[_Futures] = [
            [
                [
                    [_submit(searcher.search, f, str(facet)) for f in alt]
                    for alt in need
                ]
                for group in groups
                for need in _needs(group, dataset)
            ]
            for dataset in datasets
        ]
        searcher.wait_for(
            facet,
            [f for fs in futures for n in fs for alt in n for f in alt],
        )
        datasets = [
            {**dataset, facet: value}
            for dataset, dataset_futures in zip(datasets, futures, strict=True)
            for value in _found(dataset_futures)
            if fnmatchcase(value, str(pattern))
        ]
    return datasets


def _with_ranges(
    datasets: list[dict[object, object]],
    entry: dict[object, object],
) -> list[dict[object, object]]:
    """Write ensemble members and sub-experiments as ranges.

    The facets are in the order of the entry that the datasets replace.
    """
    recipe = datasets_to_recipe(
        [
            member
            for d in datasets
            for member in Dataset(
                **{
                    "short_name": "_",
                    **{str(k): cast("FacetValue", v) for k, v in d.items()},
                    "diagnostic": "_",
                    "variable_group": "_",
                },
            ).from_ranges()
        ],
    )
    # The facets that all datasets have in common are moved to the group.
    common = dict(recipe["diagnostics"]["_"]["variables"]["_"])
    del common["variable_group"]
    datasets = [{**common, **dataset} for dataset in recipe["datasets"]]
    return [{**{k: d[k] for k in entry if k in d}, **d} for d in datasets]


class _Dumper(yaml.SafeDumper):
    """Write YAML without anchors and aliases, with lists on one line."""

    @override
    def ignore_aliases(self, data: object) -> bool:
        return True

    @override
    def represent_sequence(
        self,
        tag: str,
        sequence: Iterable[object],
        flow_style: bool | None = None,
    ) -> yaml.SequenceNode:
        return super().represent_sequence(tag, sequence, flow_style=True)


def _text(dataset: dict[object, object], *, flow: bool) -> str:
    """Write a dataset as a flow mapping or as a block mapping."""
    text = yaml.dump(
        dataset,
        Dumper=_Dumper,
        default_flow_style=flow,
        sort_keys=False,
        allow_unicode=True,
        width=sys.maxsize,
    )
    return text.removesuffix("\n")


def _end(node: yaml.Node) -> yaml.Mark:
    """Find the end of a node, without the lines after a block collection."""
    if isinstance(node, yaml.MappingNode | yaml.SequenceNode) and (
        not node.flow_style and node.value
    ):
        last = node.value[-1]
        return _end(last[1] if isinstance(last, tuple) else last)
    return node.end_mark


def _edit(
    sequence: yaml.Node,
    node: yaml.Node,
    datasets: list[dict[object, object]],
) -> types.TextEdit:
    """Replace an item of a sequence by datasets in the style of the item."""
    flow = bool(node.flow_style) or not isinstance(node, yaml.MappingNode)
    column = node.start_mark.column
    items = [
        _text(d, flow=flow).replace("\n", "\n" + " " * column)
        for d in datasets
    ]
    if sequence.flow_style:
        separator = ", "
    else:
        # The new items start at the same column as the item.
        dash = sequence.start_mark.column
        separator = f"\n{' ' * dash}-{' ' * (column - dash - 1)}"
    end = _end(node)
    return types.TextEdit(
        range=types.Range(
            start=types.Position(
                line=node.start_mark.line,
                character=column,
            ),
            end=types.Position(line=end.line, character=end.column),
        ),
        new_text=separator.join(items),
    )


def _is_plain(text: str, parent: yaml.Node, node: yaml.Node) -> bool:
    """Check that an item of a collection has no anchors or aliases.

    The edits would remove the anchors and replace the aliases by their
    values, so such items are not replaced.
    """
    # The node of an alias is the node of its anchor, earlier in the text.
    start = node.start_mark.index
    if start < parent.start_mark.index or text[start] == "&":
        return False
    if isinstance(node, yaml.SequenceNode):
        return all(_is_plain(text, node, item) for item in node.value)
    if isinstance(node, yaml.MappingNode):
        return all(
            key.tag != "tag:yaml.org,2002:merge"
            and _is_plain(text, node, key)
            and _is_plain(text, node, value)
            for key, value in node.value
        )
    return True


def populate_edits(
    text: str,
    path: Path,
    search: Search,
    progress: Progress | None = None,
) -> Populated:
    """Replace the datasets with glob patterns by the datasets with data.

    Parameters
    ----------
    text:
        The YAML text of the recipe.
    path:
        The path of the list of datasets.
    search:
        Finds the values of facets.
    progress:
        Reports the progress of the search, if given.

    Returns
    -------
    :
        The edits.

    Raises
    ------
    SearchError
        If the datasets cannot be found.
    SearchCancelledError
        If the user cancelled the search.
    yaml.YAMLError
        If the text is not valid YAML.
    """
    recipe = yaml.safe_load(text)
    entries = data_at(recipe, path)
    nodes = _nodes(yaml.compose(text, Loader=yaml.SafeLoader), path)
    groups = _groups(recipe, path)
    if not groups or nodes is None or not isinstance(entries, list):
        msg = "No variable groups use these datasets."
        raise SearchError(msg)
    sequence = nodes[1]
    items = _replaceable(text, entries, sequence)
    steps = sum(len(_patterns(entry)) for entry, _ in items)
    searcher = _Searcher(search, progress or Progress(), steps=steps)
    edits = []
    for entry, node in items:
        datasets = _expand(searcher, groups, entry)
        if not datasets:
            msg = f"No data found for {_text(entry, flow=True)}."
            raise SearchError(msg)
        edits.append(_edit(sequence, node, _with_ranges(datasets, entry)))
    return Populated(edits, complete=searcher.complete)
