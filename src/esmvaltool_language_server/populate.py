"""Replace datasets with glob patterns by the datasets that have data.

For example, ``{dataset: '*', project: CMIP6, exp: historical, ensemble:
r1i1p1f1}`` in a list of datasets is replaced by the datasets of CMIP6 that
have data for every variable group that uses the list.
The datasets are found with one search for each variable group, by
:meth:`esmvaltool_language_server.search.FacetSearch.submit_datasets`, and
ensemble members and
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

from esmvaltool_language_server import timerange, yaml_loader
from esmvaltool_language_server.schema import data_at

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping

    from esmvalcore.typing import FacetValue

    from esmvaltool_language_server.context import Path


# The values of the facets with glob patterns of each dataset.
_Rows = list[tuple[str, ...]]


class Search(Protocol):
    """Find datasets.

    Refer to :class:`esmvaltool_language_server.search.FacetSearch`.
    """

    def submit_datasets(
        self,
        project: str,
        facets: Mapping[object, object],
        fields: tuple[str, ...],
    ) -> Future[_Rows] | None:
        """Start finding the values of facets of the datasets of a project."""

    def problems(self, future: Future[_Rows]) -> list[str]:
        """Describe why sources of the values could not be searched."""

    def cancel(self, futures: Collection[Future[_Rows]]) -> None:
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
    problems: list[str] = dataclasses.field(default_factory=list)
    """Why sources of data could not be searched, so datasets may be
    missing."""
    missing: list[str] = dataclasses.field(default_factory=list)
    """The datasets that were not replaced because no data was found."""


# A glob pattern, as in :mod:`esmvalcore.dataset`.
_PATTERN = re.compile(r".*[\*\?]+.*|.*\[.*\].*")

# The properties of a node with an anchor: an optional tag and the anchor.
_ANCHOR = re.compile(r"(?:!\S*\s+)?&")


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
        recipe = yaml_loader.load(text)
        root = yaml_loader.compose(text)
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


def _groups(recipe: object, path: Path) -> dict[str, dict[object, object]]:
    """Get the facets of the variable groups that use a list of datasets."""
    groups = {}
    diagnostics = data_at(recipe, ("diagnostics",))
    for diagnostic in diagnostics if isinstance(diagnostics, dict) else {}:
        variables = data_at(recipe, ("diagnostics", diagnostic, "variables"))
        for group in variables if isinstance(variables, dict) else {}:
            group_path = ("diagnostics", diagnostic, "variables", group)
            if group_path[: len(path) - 1] == path[:-1]:
                facets = data_at(recipe, group_path)
                facets = facets if isinstance(facets, dict) else {}
                groups[str(group)] = {"short_name": group, **facets}
    return groups


class SearchError(Exception):
    """The datasets cannot be found."""


class SearchCancelledError(Exception):
    """The user cancelled the search."""


def _submit(
    search: Search,
    facets: dict[object, object],
    fields: tuple[str, ...],
) -> Future[_Rows]:
    """Start finding the values of facets of datasets."""
    project = facets.get("project")
    future = (
        None
        if not isinstance(project, str)
        else search.submit_datasets(project, facets, fields)
    )
    if future is None:
        names = ", ".join(f"'{f}'" for f in fields)
        msg = f"Cannot search for the values of {names}."
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
    The sources of the data check that there is data for each experiment of
    a list and for each year of the time range.
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
    """The number of datasets with glob patterns to search for."""
    step: int = 0
    problems: set[str] = dataclasses.field(default_factory=set)

    def wait_for(
        self,
        entry: dict[object, object],
        futures: Mapping[Future[_Rows], set[str]],
    ) -> None:
        """Wait for the searches of the variable groups for an entry."""
        pending = set(futures)
        total = len(pending)
        what = _text(entry, flow=True)
        if self.steps > 1:
            what = f"{self.step + 1} of {self.steps}, {what}"
        while pending:
            if self.progress.cancelled():
                self.search.cancel(pending)
                raise SearchCancelledError
            groups = ", ".join(
                sorted(set().union(*(futures[f] for f in pending)))
            )
            self.progress.report(
                f"{what}: searching for {groups}",
                int(100 * (self.step + 1 - len(pending) / total) / self.steps),
            )
            _, pending = wait(
                pending, timeout=0.2, return_when=FIRST_COMPLETED
            )
        self.step += 1
        for future in futures:
            self.problems.update(self.search.problems(future))


# The searches for the data that a dataset needs: all of the outer lists,
# from any of the alternatives, all searches of an alternative.
_Futures = list[list[list[Future[_Rows]]]]


def _found(futures: _Futures) -> _Rows:
    """Get the datasets with all the data that they need."""
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
    groups: dict[str, dict[object, object]],
    entry: dict[object, object],
) -> list[dict[object, object]]:
    """Replace the glob patterns of a dataset by the values with data.

    The values of all facets with glob patterns are searched at once, and
    only the datasets with all the data that they need for every group are
    kept.
    """
    patterns = _patterns(entry)
    fields = tuple(str(facet) for facet, _ in patterns)
    futures: _Futures = []
    # The variable groups that wait for each search.
    names: dict[Future[_Rows], set[str]] = {}
    for name, group in groups.items():
        for need in _needs(group, entry):
            futures.append(
                [
                    [_submit(searcher.search, f, fields) for f in a]
                    for a in need
                ]
            )
            for future in (f for alt in futures[-1] for f in alt):
                names.setdefault(future, set()).add(name)
    searcher.wait_for(entry, names)
    return [
        {**entry, **dict(zip(fields, row, strict=True))}
        for row in _found(futures)
        if all(
            fnmatchcase(value, str(pattern))
            for value, (_, pattern) in zip(row, patterns, strict=True)
        )
    ]


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


def _end_mark(node: yaml.Node) -> yaml.Mark:
    """Find the end of a node, without the lines after a block collection."""
    if isinstance(node, yaml.MappingNode | yaml.SequenceNode) and (
        not node.flow_style and node.value
    ):
        last = node.value[-1]
        return _end_mark(last[1] if isinstance(last, tuple) else last)
    return node.end_mark


def _end(text: str, node: yaml.Node) -> types.Position:
    """Find the end of a node, without the line breaks after it.

    A block scalar, e.g. ``|``, ends at the start of the line after it.
    """
    mark = _end_mark(node)
    index = mark.index
    while index > 0 and text[index - 1].isspace():
        index -= 1
    line_start = text.rfind("\n", 0, index) + 1
    return types.Position(
        line=mark.line - text.count("\n", index, mark.index),
        character=index - line_start,
    )


def _edit(
    text: str,
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
    return types.TextEdit(
        range=types.Range(
            start=types.Position(
                line=node.start_mark.line,
                character=column,
            ),
            end=_end(text, node),
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
    if start < parent.start_mark.index or _ANCHOR.match(text, start):
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
        Finds the datasets.
    progress:
        Reports the progress of the search, if given.

    Returns
    -------
    :
        The edits. Datasets without data are not replaced.

    Raises
    ------
    SearchError
        If the datasets cannot be searched for.
    SearchCancelledError
        If the user cancelled the search.
    yaml.YAMLError
        If the text is not valid YAML.
    """
    recipe = yaml_loader.load(text)
    entries = data_at(recipe, path)
    nodes = _nodes(yaml_loader.compose(text), path)
    groups = _groups(recipe, path)
    if not groups or nodes is None or not isinstance(entries, list):
        msg = "No variable groups use these datasets."
        raise SearchError(msg)
    sequence = nodes[1]
    items = _replaceable(text, entries, sequence)
    searcher = _Searcher(search, progress or Progress(), steps=len(items))
    edits, missing = [], []
    for entry, node in items:
        if datasets := _expand(searcher, groups, entry):
            edits.append(
                _edit(text, sequence, node, _with_ranges(datasets, entry)),
            )
        else:
            missing.append(_text(entry, flow=True))
    return Populated(
        edits,
        problems=sorted(searcher.problems),
        missing=missing,
    )
