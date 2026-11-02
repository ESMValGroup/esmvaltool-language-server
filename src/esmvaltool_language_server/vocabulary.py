"""Describe the values of facets with the controlled vocabulary of a project.

The controlled vocabulary of a project is the ``<project>_CV.json`` file in
its CMOR tables, for example ``CMIP6_CV.json`` for CMIP6. Projects that use
the CMOR tables of another project, such as OBS6, do not use its controlled
vocabulary.
"""

from __future__ import annotations

import functools
import json
import re
from typing import TYPE_CHECKING

from esmvalcore.cmor.table import get_tables
from esmvalcore.config import CFG

from esmvaltool_language_server.cmor import dataset_facets, is_name

if TYPE_CHECKING:
    from esmvaltool_language_server.context import Path

# The entries of the controlled vocabulary that describe each facet.
_VOCABULARIES = {
    "activity": "activity_id",
    "dataset": "source_id",
    "exp": "experiment_id",
    "frequency": "frequency",
    "grid": "grid_label",
    "institute": "institution_id",
    "region": "region",
}

# Separators of the components of a model in its description: new lines
# (CMIP6) or semicolons that are not in parentheses (CMIP7).
_SEPARATOR = re.compile(r"\n|;\s+(?![^()]*\))")

# A variant label, e.g. r1i1p1f1.
_VARIANT = re.compile(r"r(\d+)i(\d+)p(\d+)f(\d+)")

# The Essential Model Documentation of the models of CMIP7.
_EMD_URL = "https://wcrp-cmip.github.io/Essential-Model-Documentation/model"


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
    try:
        paths = getattr(get_tables(CFG, project), "paths", [])
    except ValueError:
        return {}
    vocabulary: dict[str, object] = {}
    for path in paths:
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


def _item(entry: object, key: str) -> object:
    """Get an item of an entry that is a mapping."""
    return entry.get(key) if isinstance(entry, dict) else None


def _text(entry: object, *keys: str) -> str | None:
    """Get an entry that is text, or the first of its items that is text."""
    if isinstance(entry, str):
        return entry or None
    for key in keys:
        if isinstance(value := _item(entry, key), str) and value:
            return value
    return None


def _join(items: object) -> str | None:
    if isinstance(items, list) and items:
        return ", ".join(str(item) for item in items)
    return None


def _describe_source(project: str, name: str, entry: object) -> str | None:
    """Describe a model by its components and institutions."""
    source = _text(entry, "source")
    if source is None:
        return None
    heading, *components = (
        part.strip() for part in _SEPARATOR.split(source) if part.strip()
    )
    # CMIP7 describes a model as "<name>: <component>; <component>; ...".
    if heading.startswith(prefix := f"{name}: "):
        components.insert(0, heading.removeprefix(prefix))
        heading = ""
    parts = [heading] if heading else []
    if components:
        parts.append("\n".join(f"- {component}" for component in components))
    if institutions := _join(_item(entry, "institution_id")):
        parts.append(f"Institution: {institutions}")
    if project == "CMIP7":
        url = f"{_EMD_URL}/{name.lower()}"
        parts.append(f"[Essential Model Documentation]({url})")
    return "\n\n".join(parts)


def _describe_experiment(entry: object) -> str | None:
    """Describe an experiment with its activity and parent experiment."""
    description = _text(entry, "description", "experiment")
    if description is None:
        return None
    details = [
        f"{label}: {value}"
        for label, key in (
            ("Activity", "activity_id"),
            ("Parent experiment", "parent_experiment_id"),
        )
        if (value := _join(_item(entry, key))) and value != "no parent"
    ]
    return "\n\n".join([description, *details])


def _describe_variant(value: str) -> str | None:
    if match := _VARIANT.fullmatch(value):
        names = ("Realization", "initialization", "physics", "forcing")
        return (
            ", ".join(
                f"{name} {index}"
                for name, index in zip(names, match.groups(), strict=True)
            )
            + "."
        )
    return None


def describe(project: str, facet: str, value: str) -> str | None:
    """Describe the value of a facet with the controlled vocabulary.

    Parameters
    ----------
    project:
        The project of the dataset.
    facet:
        The facet, e.g. ``dataset`` or ``exp``.
    value:
        The value of the facet.

    Returns
    -------
    :
        Markdown text, or :obj:`None` if the controlled vocabulary of the
        project does not describe the value.
    """
    vocabulary = controlled_vocabulary(project)
    if facet == "ensemble" and "realization_index" in vocabulary:
        description = _describe_variant(value)
    elif facet in _VOCABULARIES:
        entry = _item(vocabulary.get(_VOCABULARIES[facet]), value)
        if facet == "dataset":
            description = _describe_source(project, value, entry)
        elif facet == "exp":
            description = _describe_experiment(entry)
        else:
            description = _text(entry, "description")
    else:
        description = None
    if description is None:
        return None
    # Start lists on a new line.
    separator = "\n\n" if description.startswith("- ") else " "
    return f"`{value}`:{separator}{description}"


def facet_documentation(path: Path, value: str, recipe: object) -> str | None:
    """Describe the value of a facet at a path in a recipe.

    Parameters
    ----------
    path:
        The path of the facet, or of an item in a list of values of a facet.
    value:
        The value.
    recipe:
        The recipe, used to find the project of the dataset.

    Returns
    -------
    :
        The description in the controlled vocabulary of the first project
        that describes the value, or :obj:`None`.
    """
    if path and isinstance(path[-1], int):
        path = path[:-1]
    if not path or not isinstance(facet := path[-1], str):
        return None
    projects = {
        str(project)
        for facets in dataset_facets(path[:-1], recipe)
        if is_name(project := facets.get("project"))
    }
    for project in sorted(projects):
        if (description := describe(project, facet, value)) is not None:
            return description
    return None
