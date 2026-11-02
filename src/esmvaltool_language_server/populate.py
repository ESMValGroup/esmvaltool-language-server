"""Replace datasets with glob patterns by the datasets that have data.

For example, ``{dataset: '*', project: CMIP6, exp: historical, ensemble:
r1i1p1f1}`` in a list of datasets is replaced by the datasets of CMIP6 that
have data for every variable group that uses the list.
The values of the facets with glob patterns are found with the facet search
of :mod:`esmvaltool_language_server.search`, and ensemble members and
sub-experiments are written as ranges by
:func:`esmvalcore.dataset.datasets_to_recipe`.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, cast

import yaml
from esmvalcore.dataset import Dataset, datasets_to_recipe
from esmvalcore.recipe import format_recipe
from lsprotocol import types

from esmvaltool_language_server.schema import data_at

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from concurrent.futures import Future

    from esmvalcore.typing import FacetValue

    from esmvaltool_language_server.context import Path

    Search = Callable[
        [str, Mapping[object, object], str],
        Future[list[str]] | None,
    ]
    """Start finding the values of a facet of a project, see
    :meth:`esmvaltool_language_server.search.FacetSearch.submit`."""

# Facets that :func:`esmvalcore.dataset.datasets_to_recipe` writes as ranges.
# They have the most values, so they are searched last to search less often.
_RANGES = ("ensemble", "sub_experiment")

# A glob pattern, as in :mod:`esmvalcore.dataset`.
_PATTERN = re.compile(r".*[\*\?]+.*|.*\[.*\].*")


def _is_pattern(value: object) -> bool:
    return isinstance(value, str) and bool(_PATTERN.match(value))


def _has_patterns(entry: object) -> bool:
    return isinstance(entry, dict) and any(map(_is_pattern, entry.values()))


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
            and any(map(_has_patterns, entries))
            and nodes is not None
        ):
            result.append((path, nodes[0].start_mark.line))
    return result


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
        else search(project, facets, facet)
    )
    if future is None:
        msg = f"Cannot search for the values of '{facet}'."
        raise SearchError(msg)
    return future


def _expand(
    search: Search,
    groups: list[dict[object, object]],
    entry: dict[object, object],
) -> list[dict[object, object]]:
    """Replace the glob patterns of a dataset by the values with data.

    The values of one facet are searched at a time, for all groups at once,
    and only the datasets with data for every group are kept.
    """
    found: list[list[dict[object, object]]] = [[entry] for _ in groups]
    patterns = [(k, v) for k, v in entry.items() if _is_pattern(v)]
    for facet, pattern in sorted(patterns, key=lambda p: p[0] in _RANGES):
        futures = [
            [
                (dataset, _submit(search, {**group, **dataset}, str(facet)))
                for dataset in datasets
            ]
            for group, datasets in zip(groups, found, strict=True)
        ]
        found = [
            [
                {**dataset, facet: value}
                for dataset, future in group_futures
                for value in future.result()
                if fnmatchcase(value, str(pattern))
            ]
            for group_futures in futures
        ]
    first, *others = found or [[]]
    return [d for d in first if all(d in datasets for datasets in others)]


def _with_ranges(datasets: list[dict[object, object]]) -> list[dict]:
    """Write ensemble members and sub-experiments as ranges."""
    recipe = datasets_to_recipe(
        [
            Dataset(
                **{
                    "short_name": "_",
                    **{str(k): cast("FacetValue", v) for k, v in d.items()},
                    "diagnostic": "_",
                    "variable_group": "_",
                },
            )
            for d in datasets
        ],
    )
    # The facets that all datasets have in common are moved to the group.
    common = dict(recipe["diagnostics"]["_"]["variables"]["_"])
    del common["variable_group"]
    return [{**common, **entry} for entry in recipe["datasets"]]


def _texts(datasets: list[dict]) -> list[str]:
    """Write datasets as in :func:`esmvalcore.recipe.format_recipe`."""
    text = format_recipe(yaml.safe_dump({"datasets": datasets}))
    return [line.removeprefix("  - ") for line in text.splitlines()[1:]]


def _end(node: yaml.Node) -> yaml.Mark:
    """Find the end of a node, without the lines after a block collection."""
    if isinstance(node, yaml.MappingNode | yaml.SequenceNode) and (
        not node.flow_style and node.value
    ):
        last = node.value[-1]
        return _end(last[1] if isinstance(last, tuple) else last)
    return node.end_mark


def _edit(
    text: str,
    node: yaml.Node,
    items: list[str],
    *,
    flow: bool,
) -> types.TextEdit:
    """Replace an item of a sequence by other items."""
    if flow:
        new_text = ", ".join(items)
    else:
        dash = text.rindex("-", 0, node.start_mark.index)
        column = dash - text.rfind("\n", 0, dash) - 1
        new_text = f"\n{' ' * column}- ".join(items)
    end = _end(node)
    return types.TextEdit(
        range=types.Range(
            start=types.Position(
                line=node.start_mark.line,
                character=node.start_mark.column,
            ),
            end=types.Position(line=end.line, character=end.column),
        ),
        new_text=new_text,
    )


def _is_plain(text: str, sequence: yaml.Node, node: yaml.Node) -> bool:
    """Check that an item of a sequence is not an anchor or an alias."""
    # The node of an alias is the node of its anchor, elsewhere in the text.
    start = node.start_mark.index
    return (
        sequence.start_mark.index <= start < sequence.end_mark.index
        and text[start] != "&"
    )


def populate_edits(
    text: str,
    path: Path,
    search: Search,
) -> list[types.TextEdit]:
    """Replace the datasets with glob patterns by the datasets with data.

    Parameters
    ----------
    text:
        The YAML text of the recipe.
    path:
        The path of the list of datasets.
    search:
        Starts finding the values of a facet, see
        :meth:`esmvaltool_language_server.search.FacetSearch.submit`.

    Returns
    -------
    :
        The edits, with positions that count characters.

    Raises
    ------
    SearchError
        If the datasets cannot be found.
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
    edits = []
    for entry, node in zip(entries, sequence.value, strict=True):
        if not _has_patterns(entry) or not _is_plain(text, sequence, node):
            continue
        datasets = _expand(search, groups, entry)
        if not datasets:
            msg = f"No data found for {_texts([entry])[0]}."
            raise SearchError(msg)
        items = _texts(_with_ranges(datasets))
        flow = bool(sequence.flow_style)
        edits.append(_edit(text, node, items, flow=flow))
    return edits
