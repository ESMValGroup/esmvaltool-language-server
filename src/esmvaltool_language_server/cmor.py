"""Look up variables in the CMOR tables of the configured projects."""

from __future__ import annotations

import fnmatch
import functools
from typing import TYPE_CHECKING

from esmvalcore.cmor.table import get_tables
from esmvalcore.config import CFG

from esmvaltool_language_server.schema import child, data_at

if TYPE_CHECKING:
    from esmvalcore.cmor.table import VariableInfo

    from esmvaltool_language_server.context import Path

_GLOB_CHARS = frozenset("*?[")


def is_name(value: object) -> bool:
    """Check if a facet value is a single name, not a pattern or list.

    Parameters
    ----------
    value:
        The facet value.

    Returns
    -------
    :
        Whether the value is a string without glob patterns.
    """
    return isinstance(value, str) and not _GLOB_CHARS & set(value)


def _mappings(data: object) -> list[dict[object, object]]:
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _as_mapping(data: object) -> dict[object, object]:
    return data if isinstance(data, dict) else {}


def _variable_groups(
    recipe: object,
    diagnostic: str | None = None,
) -> list[tuple[str, str, dict[object, object]]]:
    """Get the variable groups of one or all diagnostics with their facets."""
    diagnostics = _as_mapping(child(recipe, "diagnostics"))
    names = list(diagnostics) if diagnostic is None else [diagnostic]
    return [
        (str(name), str(group), _as_mapping(variable))
        for name in names
        for group, variable in _as_mapping(
            child(child(diagnostics, name), "variables"),
        ).items()
    ]


def _datasets(
    recipe: object,
    diagnostic: str,
    variable: dict[object, object],
) -> list[dict[object, object]]:
    """Get the datasets of a variable group, like ESMValCore does."""
    return (
        _mappings(child(recipe, "datasets"))
        + _mappings(
            data_at(recipe, ("diagnostics", diagnostic, "additional_datasets"))
        )
        + _mappings(child(variable, "additional_datasets"))
    )


def dataset_facets(path: Path, recipe: object) -> list[dict[object, object]]:
    """Get the facets of the datasets that the mapping at a path is part of.

    The facets of a dataset override those of the variable group, like in
    ESMValCore.

    Parameters
    ----------
    path:
        The path of a variable group or a dataset.
    recipe:
        The recipe.

    Returns
    -------
    :
        The facets of each dataset. Empty if the path is not the path of a
        variable group or a dataset.
    """
    match path:
        case ("diagnostics", str(diagnostic), "variables", str(group)):
            variable = _as_mapping(data_at(recipe, path))
            pairs = [
                (group, variable, dataset)
                for dataset in _datasets(recipe, diagnostic, variable) or [{}]
            ]
        case (
            "diagnostics",
            str(),
            "variables",
            str(group),
            "additional_datasets",
            int(),
        ):
            variable = _as_mapping(data_at(recipe, path[:4]))
            dataset = _as_mapping(data_at(recipe, path))
            pairs = [(group, variable, dataset)]
        case ("diagnostics", str(diagnostic), "additional_datasets", int()):
            dataset = _as_mapping(data_at(recipe, path))
            pairs = [
                (group, variable, dataset)
                for _, group, variable in _variable_groups(recipe, diagnostic)
            ]
        case ("datasets", int()):
            dataset = _as_mapping(data_at(recipe, path))
            pairs = [
                (group, variable, dataset)
                for _, group, variable in _variable_groups(recipe)
            ]
        case _:
            return []
    return [
        {"short_name": group, **variable, **dataset}
        for group, variable, dataset in pairs
    ]


@functools.cache
def _branded_variables(project: str) -> dict[str, dict[str, dict[str, str]]]:
    """Get the branded variables in each table of a project.

    Returns a mapping from the table to the short name to the branding
    suffix to a description of the variable.
    """
    try:
        tables = getattr(get_tables(CFG, project), "tables", None)
    except ValueError:
        return {}
    result: dict[str, dict[str, dict[str, str]]] = {}
    for mip, table in (tables or {}).items():
        for name, vardef in table.items():
            prefix = f"{vardef.short_name}_"
            if name.startswith(prefix):
                result.setdefault(mip, {}).setdefault(vardef.short_name, {})[
                    name.removeprefix(prefix)
                ] = _describe(mip, name, vardef)
    return result


def _describe(mip: str, name: str, vardef: VariableInfo) -> str:
    return "\n\n".join(
        [
            f"`{name}` in table `{mip}`: {vardef.long_name}",
            f"Units: `{vardef.units}`",
            f"Dimensions: `{' '.join(vardef.dimensions)}`",
        ]
    )


def branding_suffixes(path: Path, recipe: object) -> dict[str, str]:
    """Get the branding suffixes of the variables at a path in a recipe.

    Parameters
    ----------
    path:
        The path of a ``branding_suffix`` key in a variable group or dataset.
    recipe:
        The recipe.

    Returns
    -------
    :
        The branding suffixes, with a description of each variable, of the
        combinations of project, MIP, and short name that apply. Empty if
        these could not be found.
    """
    if not path or path[-1] != "branding_suffix":
        return {}
    suffixes: dict[str, str] = {}
    for facets in dataset_facets(path[:-1], recipe):
        project = facets.get("project")
        mip = facets.get("mip")
        short_name = facets.get("short_name")
        if not (is_name(project) and is_name(short_name)) or not isinstance(
            mip, str
        ):
            continue
        tables = _branded_variables(str(project))
        for table in [t for t in tables if fnmatch.fnmatchcase(t, mip)]:
            for suffix, text in tables[table].get(str(short_name), {}).items():
                suffixes.setdefault(suffix, text)
    return dict(sorted(suffixes.items()))
