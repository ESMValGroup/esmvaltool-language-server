from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from esmvaltool_language_server.context import context_at

if TYPE_CHECKING:
    from collections.abc import Callable

    from lsprotocol import types

RECIPE = dedent("""\
    datasets:
      - {dataset: ACCESS-CM2, project: CMIP6}
      - dataset: CanESM5
        project: CMIP6
    preprocessors:
      pp:
        extract_region:
          start_longitude: 0
    diagnostics:
      diag:
        variables:
          tas:
            preprocessor: pp
        scripts:
          script1:
            script: examples/diagnostic.py
            ancestors: [diag/tas]
    """)


@pytest.mark.parametrize(
    ("line", "path", "kind", "word"),
    [
        ("|datasets:", (), "key", "datasets"),
        ("  pp|:", ("preprocessors",), "key", "pp"),
        (
            "      start_lon|gitude: 0",
            ("preprocessors", "pp", "extract_region"),
            "key",
            "start_longitude",
        ),
        ("    project: CM|IP6", ("datasets", 1, "project"), "value", "CMIP6"),
        ("  - data|set: CanESM5", ("datasets", 1), "key", "dataset"),
        (
            "  - {dataset: ACC|ESS-CM2, project: CMIP6}",
            ("datasets", 0, "dataset"),
            "value",
            "ACCESS-CM2",
        ),
        (
            "  - {dataset: ACCESS-CM2, pro|ject: CMIP6}",
            ("datasets", 0),
            "key",
            "project",
        ),
        (
            "        ancestors: [diag/t|as]",
            ("diagnostics", "diag", "scripts", "script1", "ancestors", 0),
            "value",
            "diag/tas",
        ),
        (
            "        preprocessor: |pp",
            ("diagnostics", "diag", "variables", "tas", "preprocessor"),
            "value",
            "pp",
        ),
    ],
)
def test_context_at(
    split_cursor: Callable[[str], tuple[str, types.Position]],
    line: str,
    path: tuple[str | int, ...],
    kind: str,
    word: str,
) -> None:
    assert line.replace("|", "") in RECIPE
    text, position = split_cursor(RECIPE.replace(line.replace("|", ""), line))
    context = context_at(text, position.line, position.character)
    assert context is not None
    assert context.path == path
    assert context.kind == kind
    assert context.word == word


def test_context_at_new_key(
    split_cursor: Callable[[str], tuple[str, types.Position]],
) -> None:
    text, position = split_cursor(
        RECIPE.replace("    project: CMIP6\n", "    project: CMIP6\n    m|\n"),
    )
    context = context_at(text, position.line, position.character)
    assert context is not None
    assert context.path == ("datasets", 1)
    assert context.kind == "key"
    assert context.word == "m"


def test_context_at_compact_sequence(
    split_cursor: Callable[[str], tuple[str, types.Position]],
) -> None:
    text, position = split_cursor(
        "datasets:\n- dataset: A\n- dataset: B\n  p|"
    )
    context = context_at(text, position.line, position.character)
    assert context is not None
    assert context.path == ("datasets", 1)


def test_context_at_item(
    split_cursor: Callable[[str], tuple[str, types.Position]],
) -> None:
    text, position = split_cursor("documentation:\n  authors:\n    - and|")
    context = context_at(text, position.line, position.character)
    assert context is not None
    assert context.path == ("documentation", "authors", 0)
    assert context.kind == "item"


def test_context_at_comment(
    split_cursor: Callable[[str], tuple[str, types.Position]],
) -> None:
    text, position = split_cursor("datasets:  # a com|ment\n")
    assert context_at(text, position.line, position.character) is None


@pytest.mark.parametrize("indent", ["  ", "     "])
def test_context_at_multiline_flow_mapping(
    split_cursor: Callable[[str], tuple[str, types.Position]],
    indent: str,
) -> None:
    text, position = split_cursor(
        "datasets:\n"
        "- {dataset: A, project: CMIP6,\n"
        f"{indent}mip: Amon}}\n"
        "- {dataset: B, project: CMIP6,\n"
        f"{indent}exp: historical, m|}}\n",
    )
    context = context_at(text, position.line, position.character)
    assert context is not None
    assert context.path == ("datasets", 1)
    assert context.kind == "key"
