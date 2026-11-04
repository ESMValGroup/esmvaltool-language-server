"""Find what a recipe refers to: preprocessors, tasks, datasets, and more.

Besides preprocessors and tasks, recipes refer to the datasets of a variable
group, diagnostic scripts, and tags.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path as FilePath
from typing import TYPE_CHECKING

import yaml

from esmvaltool_language_server.schema import child, data_at

if TYPE_CHECKING:
    from esmvaltool_language_server.context import Path

SCRIPT_SUFFIXES = (".jl", ".ncl", ".py", ".R")

# The sections of ``config-references.yml`` that items of lists in a recipe
# refer to, or ``references`` for the bibtex files.
_TAG_SECTIONS: dict[tuple[str | None, ...], str] = {
    ("documentation", "authors", None): "authors",
    ("documentation", "maintainer", None): "authors",
    ("documentation", "projects", None): "projects",
    ("documentation", "references", None): "references",
    ("diagnostics", None, "themes", None): "themes",
    ("diagnostics", None, "realms", None): "realms",
}


def _matches(path: Path, pattern: tuple[str | None, ...]) -> bool:
    """Check if a path matches a pattern, where None matches any name."""
    return len(path) == len(pattern) and all(
        expected is None or part == expected
        for part, expected in zip(path, pattern, strict=True)
    )


def is_preprocessor(path: Path) -> bool:
    """Check if a path is the preprocessor of a variable group."""
    return _matches(
        path,
        ("diagnostics", None, "variables", None, "preprocessor"),
    )


def is_ancestor(path: Path) -> bool:
    """Check if a path is an ancestor of a script."""
    return _matches(
        path,
        ("diagnostics", None, "scripts", None, "ancestors", None),
    )


def is_script(path: Path) -> bool:
    """Check if a path is the file of a diagnostic script."""
    return _matches(path, ("diagnostics", None, "scripts", None, "script"))


def is_reference_dataset(path: Path) -> bool:
    """Check if a path is the reference or alternative dataset of a group."""
    return any(
        _matches(path, ("diagnostics", None, "variables", None, key))
        for key in ("reference_dataset", "alternative_dataset")
    )


def tag_section(path: Path) -> str | None:
    """Get the section of the tags that the item at a path refers to."""
    for pattern, section in _TAG_SECTIONS.items():
        if _matches(path, pattern):
            return section
    return None


def _keys(data: object) -> list[str]:
    return [str(key) for key in data] if isinstance(data, dict) else []


def preprocessor_names(recipe: object) -> list[str]:
    """Get the names of the preprocessors in a recipe."""
    return _keys(child(recipe, "preprocessors"))


def task_names(recipe: object) -> list[str]:
    """Get the names of the tasks in a recipe that scripts can use."""
    names = []
    diagnostics = child(recipe, "diagnostics")
    for name in _keys(diagnostics):
        diagnostic = child(diagnostics, name)
        names.append(f"{name}/*")
        for group in ("variables", "scripts"):
            names.extend(
                f"{name}/{task}" for task in _keys(child(diagnostic, group))
            )
    return names


def _datasets(recipe: object, group: Path) -> list[tuple[Path, dict]]:
    """Get the datasets of a variable group, with their paths."""
    paths = [
        ("datasets",),
        (*group[:2], "additional_datasets"),
        (*group, "additional_datasets"),
    ]
    return [
        ((*path, i), entry)
        for path in paths
        if isinstance(entries := data_at(recipe, path), list)
        for i, entry in enumerate(entries)
        if isinstance(entry, dict)
    ]


def dataset_names(recipe: object, path: Path) -> list[str]:
    """Get the names and aliases of the datasets of a variable group.

    Parameters
    ----------
    recipe:
        The recipe.
    path:
        A path in the variable group, e.g. of its ``reference_dataset``.

    Returns
    -------
    :
        The names and aliases, in the order of the datasets.
    """
    names = (
        entry.get(key)
        for _, entry in _datasets(recipe, path[:4])
        for key in ("dataset", "alias")
    )
    return list(dict.fromkeys(n for n in names if isinstance(n, str)))


def dataset_paths(recipe: object, path: Path, name: str) -> list[Path]:
    """Get the paths of the datasets of a variable group with a name or alias.

    Parameters
    ----------
    recipe:
        The recipe.
    path:
        A path in the variable group, e.g. of its ``reference_dataset``.
    name:
        The name or alias of the dataset.

    Returns
    -------
    :
        The paths of the ``alias`` or ``dataset`` keys that match the name.
    """
    return [
        (*dataset_path, key)
        for dataset_path, entry in _datasets(recipe, path[:4])
        for key in ("alias", "dataset")
        if entry.get(key) == name
    ]


def task_paths(name: str) -> list[Path]:
    """Get the paths in a recipe where a task could be defined."""
    diagnostic, _, task = name.partition("/")
    if any(char in diagnostic for char in "*?["):
        return []
    if not task or any(char in task for char in "*?["):
        return [("diagnostics", diagnostic)]
    return [
        ("diagnostics", diagnostic, "variables", task),
        ("diagnostics", diagnostic, "scripts", task),
    ]


@functools.cache
def esmvaltool_directory() -> FilePath | None:
    """Find the directory of the ESMValTool package, if it is installed.

    ESMValCore looks for scripts, tags, and references in the same directory.
    """
    spec = importlib.util.find_spec("esmvaltool")
    if spec is None or spec.origin is None:
        return None
    return FilePath(spec.origin).parent


def scripts_directory() -> FilePath | None:
    """Find the diagnostic scripts of ESMValTool, if it is installed."""
    package = esmvaltool_directory()
    if package is None:
        return None
    directory = package / "diag_scripts"
    return directory if directory.is_dir() else None


@functools.cache
def tags() -> dict[str, object]:
    """Load the tags defined in ``config-references.yml`` of ESMValTool."""
    path = _config_references_file()
    if path is None:
        return {}
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return content if isinstance(content, dict) else {}


def _config_references_file() -> FilePath | None:
    package = esmvaltool_directory()
    return None if package is None else package / "config-references.yml"


def _bibtex_file(name: str) -> FilePath | None:
    package = esmvaltool_directory()
    if package is None:
        return None
    return package / "references" / f"{name}.bibtex"


@functools.cache
def _tag_lines() -> dict[str, dict[str, int]]:
    """Find the line of each tag in ``config-references.yml``."""
    path = _config_references_file()
    if path is None:
        return {}
    try:
        root = yaml.compose(path.read_text(encoding="utf-8"), yaml.SafeLoader)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(root, yaml.MappingNode):
        return {}
    return {
        str(section.value): {
            str(key.value): key.start_mark.line
            for key, _ in tags_node.value
            if isinstance(key, yaml.ScalarNode)
        }
        for section, tags_node in root.value
        if isinstance(section, yaml.ScalarNode)
        and isinstance(tags_node, yaml.MappingNode)
    }


def tag_location(section: str, name: str) -> tuple[FilePath, int] | None:
    """Find where a tag is defined, as a file and a line number from 0.

    Parameters
    ----------
    section:
        The section of the tag, e.g. ``authors`` or ``references``.
    name:
        The name of the tag.

    Returns
    -------
    :
        The bibtex file of a reference, or the line in
        ``config-references.yml`` where another tag is defined, or
        :obj:`None` if the tag is not defined.
    """
    if section == "references":
        path = _bibtex_file(name)
        return None if path is None or not path.is_file() else (path, 0)
    path = _config_references_file()
    line = _tag_lines().get(section, {}).get(name)
    return None if path is None or line is None else (path, line)


def tag_value(section: str, name: str) -> object:
    """Get what a tag stands for, or :obj:`None` if it is not defined.

    For references, this is the content of their bibtex file.
    """
    if section == "references":
        path = _bibtex_file(name)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
    content = child(tags().get(section), name)
    return content if isinstance(content, dict | str) else None


def script_file(name: str) -> FilePath | None:
    """Find the file of a diagnostic script."""
    path = FilePath(name).expanduser()
    if not path.is_absolute():
        directory = scripts_directory()
        if directory is None:
            return None
        path = directory / path
    return path if path.is_file() else None


@functools.cache
def script_names() -> list[str]:
    """Get the diagnostic scripts of ESMValTool, relative to their directory."""
    directory = scripts_directory()
    if directory is None:
        return []
    return sorted(
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.suffix in SCRIPT_SUFFIXES
        and path.name != "__init__.py"
        and path.relative_to(directory).parts[0] != "shared"
    )
