"""Look up variables in the CMOR tables of the configured projects."""

from __future__ import annotations

import fnmatch
import functools
import json
from typing import TYPE_CHECKING

import pandas as pd
from esmvalcore.cmor.table import get_tables
from esmvalcore.config import CFG

from esmvaltool_language_server.schema import child, data_at

if TYPE_CHECKING:
    from esmvalcore.cmor.table import InfoBase, TableInfo, VariableInfo

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


@functools.cache
def controlled_vocabulary(project: str) -> dict[str, object]:
    """Load the controlled vocabulary of a project from its CMOR tables.

    Parameters
    ----------
    project:
        The project.

    Returns
    -------
    :
        The controlled vocabulary, or an empty mapping if the project has
        none.
    """
    vocabulary: dict[str, object] = {}
    for path in getattr(_info(project), "paths", []):
        for file in sorted(path.glob(f"{project}_CV.json")):
            try:
                content = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(content, dict) and isinstance(
                content.get("CV"), dict
            ):
                vocabulary.update(content["CV"])
    return vocabulary


def _split_name(name: str, vardef: VariableInfo) -> tuple[str, str]:
    """Split the name of a variable in a table into short name and suffix."""
    prefix = f"{vardef.short_name}_"
    if name.startswith(prefix):
        return vardef.short_name, name.removeprefix(prefix)
    return name, ""


def _frequencies(
    project: str,
    table: TableInfo,
    vardef: VariableInfo,
) -> list[str]:
    """Get the frequencies of a variable.

    If the CMOR table does not define the frequency, e.g. in CMIP7, a
    variable with a time dimension can have any frequency of the controlled
    vocabulary except ``fx``, and a variable without one only ``fx``.
    """
    if frequency := vardef.frequency or table.frequency:
        return [frequency]
    choices = _as_mapping(controlled_vocabulary(project).get("frequency"))
    has_time = any(name.startswith("time") for name in vardef.dimensions)
    return [str(name) for name in choices if (name != "fx") == has_time] or [
        ""
    ]


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
        A table with a row for each frequency of each variable in each MIP
        table, with the columns ``mip``, ``short_name``, ``branding_suffix``,
        ``frequency``, ``realm``, and ``description``.
        If the CMOR table does not define the frequency, the frequencies of
        the controlled vocabulary are used, or an empty frequency if there
        is none.
        The realm is the one that the extra facets of the project add for
        all datasets, e.g. the MIP in CMIP7, or empty if they add none.
    """
    tables = getattr(_info(project), "tables", None) or {}
    rows = [
        (
            mip,
            short_name,
            suffix,
            frequency,
            _extra_realm(project, mip, short_name),
            _describe(mip, name, vardef),
        )
        for mip, table in tables.items()
        if mip
        for name, vardef in table.items()
        for short_name, suffix in [_split_name(name, vardef)]
        for frequency in _frequencies(project, table, vardef)
    ]
    return pd.DataFrame(rows, columns=_COLUMNS).astype("category")


def _extra_realm(project: str, mip: str, short_name: str) -> str:
    """Get the realm that the extra facets add for all datasets."""
    extra_facets = _as_mapping(
        _as_mapping(CFG["projects"].get(project)).get("extra_facets"),
    )
    realm = ""
    for mip_pattern, by_variable in _as_mapping(extra_facets.get("*")).items():
        if not fnmatch.fnmatchcase(mip, str(mip_pattern)):
            continue
        for pattern, facets in _as_mapping(by_variable).items():
            if fnmatch.fnmatchcase(short_name, str(pattern)) and is_name(
                value := _as_mapping(facets).get("realm"),
            ):
                realm = str(value)
    return realm


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

    An empty frequency or realm matches any value. Facets that are not
    a single name, e.g. a list or a glob pattern, select all values, except
    for the MIP, which can be a glob pattern.
    """
    project = str(facets["project"])
    table = variables(project)
    selected = pd.Series(data=True, index=table.index)
    if is_name(short_name := facets.get("short_name")):
        names = _alt_names(_info(project), str(short_name))
        selected &= table["short_name"].isin(names)
    if is_name(suffix := facets.get("branding_suffix")):
        selected &= table["branding_suffix"] == suffix
    for facet in ("frequency", "realm"):
        if is_name(value := facets.get(facet)):
            selected &= table[facet].isin([value, ""])
    if isinstance(mip := facets.get("mip"), str):
        selected &= (
            table["mip"]
            .map(lambda name: fnmatch.fnmatchcase(name, mip))
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
    "The variable is not in a CMOR table with this {facet}, but ESMValCore "
    "also looks for it in the other tables of the project because {reason}."
)
_FACET_NAMES = {"mip": "MIP", "frequency": "frequency", "realm": "realm"}
_VARIABLE_FACETS = frozenset({"short_name", "branding_suffix"})


def _any_table_reason(facets: dict[object, object]) -> str | None:
    """Explain why ESMValCore looks for a variable in all tables, if it does."""
    if facets.get("derive", False) is True:
        return "the variable is derived"
    project = facets["project"]
    if not getattr(_info(str(project)), "strict", True):
        return f"the CMOR checks of project {project} are not strict"
    return None


def _project_values(
    facets: dict[object, object],
    facet: str,
) -> dict[str, str]:
    """Get the values of a facet that the other facets of a dataset allow.

    Returns the values with a note on why ESMValCore allows them, or an empty
    note for the values of the tables that contain the variable. If no table
    contains the variable, the values that the other facets allow are
    returned without a note.
    """
    others = {key: value for key, value in facets.items() if key != facet}
    found = set(_matches(others)[facet]) - {""}
    any_variable = {
        key: value
        for key, value in others.items()
        if key not in _VARIABLE_FACETS
    }
    if not found:
        return dict.fromkeys(set(_matches(any_variable)[facet]) - {""}, "")
    result = dict.fromkeys(found, "")
    if (reason := _any_table_reason(facets)) is not None:
        note = _ANY_TABLE.format(facet=_FACET_NAMES[facet], reason=reason)
        for value in set(_matches(any_variable)[facet]) - found - {""}:
            result[value] = note
    return result


def _facet_at(path: Path) -> tuple[Path, str | None]:
    """Get the path of the facets and the facet at a path."""
    if path and isinstance(path[-1], int):
        path = path[:-1]
    if not path or not isinstance(path[-1], str):
        return path, None
    return path[:-1], path[-1]


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
    path, facet = _facet_at(path)
    if facet != "mip":
        return {}
    result: dict[str, str] = {}
    for facets in dataset_facets(path, recipe):
        if is_name(project := facets.get("project")):
            tables = _tables(str(project))
            for name, note in _project_values(facets, "mip").items():
                text = "\n\n".join(filter(None, [tables.get(name), note]))
                result.setdefault(name, text)
    if not result:
        for project in CFG["projects"]:
            for name, text in _tables(project).items():
                result.setdefault(name, text)
    return dict(sorted(result.items()))


def table_values(path: Path, recipe: object) -> dict[str, str]:
    """Get the frequencies or realms that apply at a path in a recipe.

    Parameters
    ----------
    path:
        The path of a ``frequency`` or ``realm`` key in a variable group,
        dataset, or supplementary variable, or of an item in a list of
        values.
    recipe:
        The recipe.

    Returns
    -------
    :
        The values of the variables in the CMOR tables of the projects that
        apply that match the other facets. Values that ESMValCore only
        allows because the project is not strict or the variable is derived
        have a note, the others an empty text. Empty if the path is not that
        of a frequency or realm, or the project is not known.
    """
    path, facet = _facet_at(path)
    if facet not in {"frequency", "realm"}:
        return {}
    result: dict[str, str] = {}
    for facets in dataset_facets(path, recipe):
        if is_name(facets.get("project")):
            for value, note in _project_values(facets, facet).items():
                result.setdefault(value, note)
    return dict(sorted(result.items()))
