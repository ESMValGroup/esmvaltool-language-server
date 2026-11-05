from __future__ import annotations

import pytest
import yaml
from lsprotocol import types

from esmvaltool_language_server.formatting import (
    formatting_edits,
    has_comments,
)


def _apply(text: str, edits: list[types.TextEdit]) -> str:
    """Apply edits with positions that count characters to a text."""
    lines = text.splitlines(keepends=True)

    def offset(position: types.Position) -> int:
        return sum(map(len, lines[: position.line])) + position.character

    for edit in sorted(
        edits, key=lambda e: offset(e.range.start), reverse=True
    ):
        start, end = offset(edit.range.start), offset(edit.range.end)
        text = text[:start] + edit.new_text + text[end:]
    return text


@pytest.mark.parametrize(
    "text",
    [
        "# Comment\na: 1\n",
        "a: 1  # Comment\n",
        "a:\n  # Comment\n  b: 1\n",
        "# Header\n---\n# Comment\na: 1\n",
        "# Header\n---\na: 1  # Comment\n",
        "# Comment\na: 1\n---\n",
    ],
)
def test_has_comments(text: str) -> None:
    assert has_comments(text)


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\n",
        "a: b#c\n",
        "a: 'b # c'\n",
        'a: "b # c"\n',
        "a: |\n  b\n  # c\n",
        "---\na: 1\n",
        "# ESMValTool\n# recipe_a.yml\n---\na: 1\n",
        "# Header\n\n  # Header\r\n---  \r\na: 1\n",
    ],
)
def test_has_no_comments(text: str) -> None:
    assert not has_comments(text)


def test_has_comments_invalid_yaml() -> None:
    with pytest.raises(yaml.YAMLError):
        has_comments("a: 'b\n")


FORMATTED = (
    "datasets:\n  - {dataset: CESM2, project: CMIP6}\n\ndiagnostics: {}\n"
)
UNFORMATTED = "datasets: [{project: CMIP6, dataset: CESM2}]\ndiagnostics: {}\n"
HEADER = "# ESMValTool\n\n# recipe_a.yml\n---\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "datasets:\n- {project: CMIP6, dataset: CESM2}\ndiagnostics: {}\n",
            FORMATTED,
        ),
        # Without a newline at the end.
        (
            "datasets: [{project: CMIP6, dataset: CESM2}]\ndiagnostics: {}",
            FORMATTED,
        ),
        (FORMATTED, FORMATTED),
        ("", "{}\n"),
        # The part up to the document start is kept.
        (f"---\n{FORMATTED}", f"---\n{FORMATTED}"),
        (HEADER + UNFORMATTED, HEADER + FORMATTED),
    ],
)
def test_formatting_edits(text: str, expected: str) -> None:
    assert _apply(text, formatting_edits(text)) == expected


def test_formatting_edits_only_changed_lines() -> None:
    text = FORMATTED.replace(
        "dataset: CESM2, project: CMIP6", "project: CMIP6, dataset: CESM2"
    )
    assert formatting_edits(text) == [
        types.TextEdit(
            range=types.Range(
                start=types.Position(line=1, character=0),
                end=types.Position(line=2, character=0),
            ),
            new_text="  - {dataset: CESM2, project: CMIP6}\n",
        ),
    ]
