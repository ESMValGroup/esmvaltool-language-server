from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from esmvalcore.recipe import format_recipe
from lsprotocol import types

from esmvaltool_language_server.formatting import formatting_edits

if TYPE_CHECKING:
    from collections.abc import Callable

    ApplyEdits = Callable[[str, list[types.TextEdit]], str]


FORMATTED = (
    "datasets:\n  - {dataset: CESM2, project: CMIP6}\n\ndiagnostics: {}\n"
)
UNFORMATTED = "datasets: [{project: CMIP6, dataset: CESM2}]\ndiagnostics: {}\n"
HEADER = "# ESMValTool\n\n# recipe_a.yml\n---\n"


@pytest.mark.parametrize(
    "text",
    [
        UNFORMATTED,
        # Without a newline at the end.
        UNFORMATTED.rstrip("\n"),
        f"---\n{UNFORMATTED}",
        HEADER + UNFORMATTED,
        "# Comment\n" + UNFORMATTED.replace("\n", "  # Datasets\n", 1),
        "documentation:\n  description: >\n    First.\n\n    Second.\n",
    ],
)
def test_formatting_edits(text: str, apply_edits: ApplyEdits) -> None:
    assert apply_edits(text, formatting_edits(text)) == format_recipe(text)


@pytest.mark.parametrize("text", ["", "\n", "# Comment\n", "---\n"])
def test_formatting_edits_without_keys(text: str) -> None:
    assert formatting_edits(text) == []


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
