"""Describe the values of facets with the controlled vocabulary of a project.

The controlled vocabulary of a project is the ``<project>_CV.json`` file in
its CMOR tables, for example ``CMIP6_CV.json`` for CMIP6. Projects that use
the CMOR tables of another project, such as OBS6, do not use its controlled
vocabulary.
Projects are described by a link to their website, or to the documentation
of ESMValTool or ESMValCore if they have none.
"""

from __future__ import annotations

import functools
import json
import re
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from esmvalcore.cmor.table import get_tables
from esmvalcore.config import CFG

from esmvaltool_language_server.cmor import dataset_facets, is_name
from esmvaltool_language_server.esgf import intake_esgf_mapping

if TYPE_CHECKING:
    from esmvaltool_language_server.context import Path

# Separators of the components of a model in its description: new lines
# (CMIP6) or semicolons that are not in parentheses (CMIP7).
_SEPARATOR = re.compile(r"\n|;\s+(?![^()]*\))")

# A variant label, e.g. r1i1p1f1.
_VARIANT = re.compile(r"r(\d+)i(\d+)p(\d+)f(\d+)")

# The websites of the projects.
_PROJECT_URLS = {
    "CMIP3": "https://wcrp-cmip.org/cmip-phases/cmip3/",
    "CMIP5": "https://wcrp-cmip.org/cmip-phases/cmip5/",
    "CMIP6": "https://wcrp-cmip.org/cmip-phases/cmip6/",
    "CMIP7": "https://wcrp-cmip.org/cmip7/",
    "CORDEX": "https://cordex.org/",
    "CORDEX-CMIP6": "https://cordex.org/experiment-guidelines/cordex-cmip6/",
    "obs4MIPs": "https://pcmdi.github.io/obs4MIPs/",
}

# The documentation of the packages, and the pages in it that describe the
# projects without a website.
_DOCS_URLS = {
    "ESMValTool": "https://docs.esmvaltool.org/en/{version}",
    "ESMValCore": "https://docs.esmvaltool.org/projects/ESMValCore/en/{version}",
}
_NATIVE_MODELS = (
    "ESMValCore",
    "quickstart/find_data.html#supported-native-models",
)
_PROJECT_DOCS = {
    "OBS": ("ESMValTool", "input.html#observations"),
    "OBS6": ("ESMValTool", "input.html#observations"),
    "native6": (
        "ESMValCore",
        "quickstart/find_data.html#read-native-datasets",
    ),
    "ACCESS": _NATIVE_MODELS,
    "CESM": _NATIVE_MODELS,
    "EMAC": _NATIVE_MODELS,
    "ICON": _NATIVE_MODELS,
    "IPSLCM": _NATIVE_MODELS,
}

# A released version, which has its own documentation.
_RELEASE = re.compile(r"\d+\.\d+\.\d+")

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


def _describe_source(
    project: str,
    name: str,
    entry: object,
    prefix: str = "",
) -> str | None:
    """Describe a model by its components and institutions.

    The prefix is ``driving_`` for the driving model of CORDEX-CMIP6.
    """
    source = _item(entry, f"{prefix}source")
    # CORDEX-CMIP6 lists descriptions with and without the year of the model.
    if isinstance(source, list) and source:
        source = source[0]
    source = _text(source)
    if source is None:
        return None
    heading, *components = (
        part.strip() for part in _SEPARATOR.split(source) if part.strip()
    )
    # CMIP7 describes a model as "<name>: <component>; <component>; ...".
    if heading.startswith(label := f"{name}: "):
        components.insert(0, heading.removeprefix(label))
        heading = ""
    parts = [heading] if heading else []
    if components:
        parts.append("\n".join(f"- {component}" for component in components))
    if institutions := _join(_item(entry, f"{prefix}institution_id")):
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


def _vocabulary_name(project: str, facet: str) -> str:
    """Get the name of the entry of the vocabulary that describes a facet.

    This is the name of the facet on ESGF, or the name of the facet itself
    if it is not used to find data on ESGF, e.g. ``frequency`` in CMIP6.
    """
    mapping = intake_esgf_mapping(project)
    return mapping.facets.get(facet, facet) if mapping else facet


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
    else:
        name = _vocabulary_name(project, facet)
        entry = _item(vocabulary.get(name), value)
        if name in {"source_id", "driving_source_id"}:
            prefix = name.removesuffix("source_id")
            description = _describe_source(project, value, entry, prefix)
        elif name == "experiment_id":
            description = _describe_experiment(entry)
        else:
            # CORDEX-CMIP6 describes e.g. a domain_id by its domain.
            description = _text(entry, "description", name.removesuffix("_id"))
    if description is None:
        return None
    # Start lists on a new line.
    separator = "\n\n" if description.startswith("- ") else " "
    return f"`{value}`:{separator}{description}"


@functools.cache
def _docs_version(package: str) -> str:
    """Get the version of the documentation of a package."""
    try:
        installed = version(package)
    except PackageNotFoundError:
        return "latest"
    return f"v{installed}" if _RELEASE.fullmatch(installed) else "latest"


def _describe_project(value: str) -> str | None:
    """Link a project to its website or documentation."""
    if url := _PROJECT_URLS.get(value):
        return f"`{value}`: [Project website]({url})"
    if value in _PROJECT_DOCS:
        package, page = _PROJECT_DOCS[value]
        docs = _DOCS_URLS[package].format(version=_docs_version(package))
        return f"`{value}`: [{package} documentation]({docs}/{page})"
    return None


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
        that describes the value, a link to the website or documentation of a
        project, or :obj:`None`.
    """
    if path and isinstance(path[-1], int):
        path = path[:-1]
    if not path or not isinstance(facet := path[-1], str):
        return None
    if facet == "project":
        return _describe_project(value)
    projects = {
        str(project)
        for facets in dataset_facets(path[:-1], recipe)
        if is_name(project := facets.get("project"))
    }
    for project in sorted(projects):
        if (description := describe(project, facet, value)) is not None:
            return description
    return None
