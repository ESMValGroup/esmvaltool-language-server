from __future__ import annotations

import pytest
from lsprotocol import types

from esmvaltool_language_server.formatting import formatting_edits


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
        # Comments are kept.
        (
            "# Comment\n" + UNFORMATTED.replace("\n", "  # Datasets\n", 1),
            "# Comment\n" + FORMATTED.replace("6}\n", "6}  # Datasets\n"),
        ),
    ],
)
def test_formatting_edits(text: str, expected: str) -> None:
    assert _apply(text, formatting_edits(text)) == expected


def test_formatting_edits_folded_description() -> None:
    text = (
        "# recipe.yml\n---\ndocumentation:\n  description: >\n"
        "    First paragraph.\n\n    Second paragraph.\n\n"
        "    - item one\n    - item two\n"
    )
    assert _apply(text, formatting_edits(text)) == (
        "# recipe.yml\n---\ndocumentation:\n  description: |-\n"
        "    First paragraph.\n\n    Second paragraph.\n\n"
        "    - item one\n    - item two\n"
    )


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
