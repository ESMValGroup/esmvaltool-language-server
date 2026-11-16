"""Look up variables in the CMOR tables of the configured projects."""

from __future__ import annotations

import fnmatch
import functools
from typing import TYPE_CHECKING

import pandas as pd
from esmvalcore.cmor.table import get_tables
from esmvalcore.config import CFG

from esmvaltool_language_server.schema import child, data_at

if TYPE_CHECKING:
    from esmvalcore.cmor.table import InfoBase, VariableInfo

    from esmvaltool_language_server.context import Path

_GLOB_CHARS = frozenset("*?[")
_COLUMNS = (
    "mip",
    "short_name",
    "branding_suffix",
    "frequency",
    "realm",
    "description",
)


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


def _pairs(
    path: Path,
    recipe: object,
) -> list[tuple[str, dict[object, object], dict[object, object]]]:
    """Get the variable groups and datasets at a path."""
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
            pairs = []
    return pairs


def _supplementary(
    variable: dict[object, object],
    dataset: dict[object, object],
    supplementary: dict[object, object],
) -> dict[object, object]:
    """Merge the definitions of a supplementary variable, like ESMValCore."""
    short_name = supplementary.get("short_name")
    merged: dict[object, object] = {}
    for item in _mappings(child(variable, "supplementary_variables")) + (
        _mappings(child(dataset, "supplementary_variables"))
    ):
        if item.get("short_name") == short_name:
            merged.update(item)
    return {**merged, **supplementary}


def dataset_facets(path: Path, recipe: object) -> list[dict[object, object]]:
    """Get the facets of the datasets that the mapping at a path is part of.

    The facets of a dataset override those of the variable group, like in
    ESMValCore. The facets of a supplementary variable override those of the
    dataset that it belongs to.

    Parameters
    ----------
    path:
        The path of a variable group, a dataset, or a supplementary variable.
    recipe:
        The recipe.

    Returns
    -------
    :
        The facets of each dataset. Empty if the path is not the path of a
        variable group, a dataset, or a supplementary variable.
    """
    supplementary = None
    match path:
        case (*parent, "supplementary_variables", int()):
            supplementary = _as_mapping(data_at(recipe, path))
            path = tuple(parent)
    facets = []
    for group, variable, dataset in _pairs(path, recipe):
        item = {"short_name": group, **variable, **dataset}
        if supplementary is not None:
            item.pop("supplementary_variables", None)
            item.update(_supplementary(variable, dataset, supplementary))
        facets.append(item)
    return facets


def _info(project: str) -> InfoBase | None:
    try:
        return get_tables(CFG, project)
    except ValueError:
        return None


def _split_name(name: str, vardef: VariableInfo) -> tuple[str, str]:
    """Split the name of a variable in a table into short name and suffix."""
    prefix = f"{vardef.short_name}_"
    if name.startswith(prefix):
        return vardef.short_name, name.removeprefix(prefix)
    return name, ""


@functools.cache
def variables(project: str) -> pd.DataFrame:
    """Get the variables in the CMOR tables of a project.

    Parameters
    ----------
    project:
        The project.

    Returns
    -------
    :
        A table with a row for each realm of each variable in each MIP table,
        with the columns ``mip``, ``short_name``, ``branding_suffix``,
        ``frequency``, ``realm``, and ``description``. The frequency is
        empty if the CMOR table does not define it, e.g. in CMIP7, where
        a variable can be at any frequency.
    """
    tables = getattr(_info(project), "tables", None) or {}
    rows = [
        (
            mip,
            *_split_name(name, vardef),
            vardef.frequency or table.frequency,
            realm,
            _describe(mip, name, vardef),
        )
        for mip, table in tables.items()
        if mip
        for name, vardef in table.items()
        for realm in vardef.modeling_realm or [table.realm]
    ]
    return pd.DataFrame(rows, columns=_COLUMNS).astype("category")


def _describe(mip: str, name: str, vardef: VariableInfo) -> str:
    return "\n\n".join(
        [
            f"`{name}` in table `{mip}`: {vardef.long_name}",
            f"Units: `{vardef.units}`",
            f"Dimensions: `{' '.join(vardef.dimensions)}`",
        ]
    )


def _alt_names(info: InfoBase | None, short_name: str) -> set[str]:
    """Get a short name and its alternative names, like ESMValCore."""
    names = {short_name}
    for group in getattr(info, "alt_names", None) or []:
        if short_name in group:
            names.update(group)
    return names


def _matches(facets: dict[object, object]) -> pd.DataFrame:
    """Get the variables in the CMOR tables that match the facets.

    The frequency only excludes variables that have a different frequency
    in the CMOR table. Facets that are not a single name, e.g. a list or
    a glob pattern, select all values, except for the MIP, which can be a
    glob pattern.
    """
    project = str(facets["project"])
    table = variables(project)
    selected = pd.Series(data=True, index=table.index)
    if is_name(short_name := facets.get("short_name")):
        names = _alt_names(_info(project), str(short_name))
        selected &= table["short_name"].isin(names)
    for facet in ("branding_suffix", "realm"):
        if is_name(value := facets.get(facet)):
            selected &= table[facet] == value
    if is_name(frequency := facets.get("frequency")):
        selected &= table["frequency"].isin([frequency, ""])
    if isinstance(mip := facets.get("mip"), str):
        selected &= (
            table["mip"]
            .map(
                lambda name: fnmatch.fnmatchcase(name, mip),
            )
            .astype(bool)
        )
    return table[selected]


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
        variables in the CMOR tables that match the facets of the variable
        group or datasets. Empty if these could not be found.
    """
    if not path or path[-1] != "branding_suffix":
        return {}
    suffixes: dict[str, str] = {}
    for facets in dataset_facets(path[:-1], recipe):
        if not (
            is_name(facets.get("project"))
            and is_name(facets.get("short_name"))
            and isinstance(facets.get("mip"), str)
        ):
            continue
        facets.pop("branding_suffix", None)
        found = _matches(facets)
        for suffix, text in zip(
            found["branding_suffix"], found["description"], strict=True
        ):
            if suffix:
                suffixes.setdefault(suffix, text)
    return dict(sorted(suffixes.items()))


def _tables(project: str) -> dict[str, str]:
    """Get the names of the tables of a project with their descriptions."""
    tables = getattr(_info(project), "tables", None)
    return {
        name: ", ".join(
            f"{label}: `{value}`"
            for label, value in (
                ("Frequency", table.frequency),
                ("Realm", table.realm),
            )
            if value
        )
        for name, table in (tables or {}).items()
        if name
    }


_ANY_TABLE = (
    "The variable is not in this table, but ESMValCore looks for it in the "
    "other tables of the project because {reason}."
)


def _project_mips(facets: dict[object, object]) -> dict[str, str]:
    """Get the MIP tables of a project that the facets of a dataset allow.

    Returns all tables of the project if the variable is not in any of them
    or the short name is not known.
    """
    project = str(facets["project"])
    tables = _tables(project)
    facets = {key: value for key, value in facets.items() if key != "mip"}
    found = set(_matches(facets)["mip"])
    if not found:
        return tables
    reason = None
    if facets.get("derive", False) is True:
        reason = "the variable is derived"
    elif not getattr(_info(project), "strict", True):
        reason = f"the CMOR checks of project {project} are not strict"
    if reason is None:
        return {name: tables[name] for name in found if name in tables}
    note = _ANY_TABLE.format(reason=reason)
    return {
        name: text if name in found else f"{text}\n\n{note}".lstrip()
        for name, text in tables.items()
    }


def mips(path: Path, recipe: object) -> dict[str, str]:
    """Get the MIP tables that apply at a path in a recipe.

    Parameters
    ----------
    path:
        The path of a ``mip`` key in a variable group, dataset, or
        supplementary variable, or of an item in a list of MIPs.
    recipe:
        The recipe.

    Returns
    -------
    :
        The names of the tables, with their frequency and realm, if known,
        of the projects that apply that contain the variable and match the
        other facets. If the project is not strict or the variable is
        derived, ESMValCore also looks for the variable in the other tables
        of the project, so these are also included, with a note.
        The tables of all configured projects if the project is not known
        or has no tables. Empty if the path is not that of a MIP.
    """
    if path and isinstance(path[-1], int):
        path = path[:-1]
    if not path or path[-1] != "mip":
        return {}
    result: dict[str, str] = {}
    for facets in dataset_facets(path[:-1], recipe):
        if is_name(facets.get("project")):
            for name, text in _project_mips(facets).items():
                result.setdefault(name, text)
    if not result:
        for project in CFG["projects"]:
            for name, text in _tables(project).items():
                result.setdefault(name, text)
    return dict(sorted(result.items()))
