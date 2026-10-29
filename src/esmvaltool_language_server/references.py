"""Find what a recipe refers to: preprocessors, tasks, scripts, and tags."""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path as FilePath
from typing import TYPE_CHECKING

import yaml

from esmvaltool_language_server.schema import child

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
    package = esmvaltool_directory()
    if package is None:
        return {}
    path = package / "config-references.yml"
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return content if isinstance(content, dict) else {}


def tag_value(section: str, name: str) -> object:
    """Get what a tag stands for, or :obj:`None` if it is not defined.

    For references, this is the content of their bibtex file.
    """
    if section == "references":
        package = esmvaltool_directory()
        if package is None:
            return None
        path = package / "references" / f"{name}.bibtex"
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
