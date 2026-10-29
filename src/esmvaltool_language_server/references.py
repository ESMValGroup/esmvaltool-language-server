"""Find what a recipe refers to: preprocessors, tasks, and scripts."""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path as FilePath
from typing import TYPE_CHECKING

from esmvaltool_language_server.schema import child

if TYPE_CHECKING:
    from esmvaltool_language_server.context import Path

SCRIPT_SUFFIXES = (".jl", ".ncl", ".py", ".R")


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
def scripts_directory() -> FilePath | None:
    """Find the diagnostic scripts of ESMValTool, if it is installed.

    ESMValCore looks for scripts in the same directory.
    """
    spec = importlib.util.find_spec("esmvaltool")
    if spec is None or spec.origin is None:
        return None
    directory = FilePath(spec.origin).parent / "diag_scripts"
    return directory if directory.is_dir() else None


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
