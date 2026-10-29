"""Tests for completion, hover, and go to definition."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest
import yaml
from lsprotocol import types

from esmvaltool_language_server import references
from esmvaltool_language_server.completion import completions
from esmvaltool_language_server.definition import definition
from esmvaltool_language_server.hover import hover
from esmvaltool_language_server.schema import MISSING, recipe_schema

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    SplitCursor = Callable[[str], tuple[str, types.Position]]

URI = "file:///recipe_test.yml"

RECIPE = dedent("""\
    documentation:
      title: Example
    preprocessors:
      pp:
        regrid:
          target_grid: 1x1
          scheme: linear
      other:
        anomalies:
          period: month
    diagnostics:
      diag:
        variables:
          tas:
            preprocessor: pp
            project: CMIP6
            mip: Amon
        scripts:
          script1:
            script: examples/diagnostic.py
            ancestors: [diag/tas]
    """)


def _complete(split_cursor: SplitCursor, old: str, new: str) -> list[str]:
    assert old in RECIPE
    text, position = split_cursor(RECIPE.replace(old, new))
    items = completions(
        text,
        position,
        yaml.safe_load(RECIPE),
        recipe_schema(),
    )
    return [item.label for item in items]


def test_complete_top_level_keys(split_cursor: SplitCursor) -> None:
    labels = _complete(split_cursor, "diagnostics:\n", "diagnostics:\nd|\n")
    assert labels == ["datasets"]


def test_complete_steps(split_cursor: SplitCursor) -> None:
    labels = _complete(
        split_cursor,
        "      scheme: linear\n",
        "      scheme: linear\n    ext|\n",
    )
    assert "extract_region" in labels
    assert "regrid" not in labels


def test_complete_step_key_text(split_cursor: SplitCursor) -> None:
    text, position = split_cursor(
        RECIPE.replace("  other:\n", "  other:\n    ext|\n")
    )
    items = completions(text, position, MISSING, recipe_schema())
    (item,) = [item for item in items if item.label == "extract_region"]
    assert isinstance(item.text_edit, types.TextEdit)
    assert item.text_edit.new_text == "extract_region: "
    assert item.text_edit.range.start.character == 4
    assert isinstance(item.documentation, types.MarkupContent)
    assert "Extract a region" in item.documentation.value


def _snippet(split_cursor: SplitCursor, old: str, new: str, label: str) -> str:
    """Get the text that completing a key inserts, with snippets."""
    assert old in RECIPE
    text, position = split_cursor(RECIPE.replace(old, new))
    items = completions(
        text, position, MISSING, recipe_schema(), snippets=True
    )
    (item,) = [item for item in items if item.label == label]
    assert isinstance(item.text_edit, types.TextEdit)
    return item.text_edit.new_text


def test_complete_required_keys(split_cursor: SplitCursor) -> None:
    text = _snippet(
        split_cursor, "  other:\n", "  other:\n    reg|\n", "regrid"
    )
    assert text == "regrid:\n      target_grid: ${1}\n      scheme: ${2}"


def test_complete_key_with_default(split_cursor: SplitCursor) -> None:
    text = _snippet(
        split_cursor,
        "  other:\n",
        "  other:\n    climate_statistics:\n      oper|\n",
        "operator",
    )
    assert text == "operator: ${1:mean}"


def test_complete_key_in_flow_mapping(split_cursor: SplitCursor) -> None:
    text = _snippet(split_cursor, "  other:\n", "  other: {reg|}\n", "regrid")
    assert text == "regrid: "


def test_complete_default_value(split_cursor: SplitCursor) -> None:
    text, position = split_cursor(
        RECIPE.replace(
            "  other:\n",
            "  other:\n    climate_statistics:\n      operator: |\n",
        ),
    )
    items = completions(text, position, MISSING, recipe_schema())
    preselected = [item.label for item in items if item.preselect]
    assert preselected == ["mean"]


def test_complete_mip(split_cursor: SplitCursor) -> None:
    labels = _complete(split_cursor, "mip: Amon", "mip: A|")
    assert "Amon" in labels
    assert "Omon" in labels


def test_complete_preprocessor(split_cursor: SplitCursor) -> None:
    labels = _complete(
        split_cursor,
        "preprocessor: pp",
        "preprocessor: |",
    )
    assert labels == ["pp", "other"]


def test_complete_ancestors(split_cursor: SplitCursor) -> None:
    labels = _complete(split_cursor, "[diag/tas]", "[diag/tas, |]")
    assert labels == ["diag/*", "diag/tas", "diag/script1"]


def test_hover_step(split_cursor: SplitCursor) -> None:
    text, position = split_cursor(RECIPE.replace("regrid:", "reg|rid:"))
    result = hover(text, position, MISSING, recipe_schema())
    assert result is not None
    assert isinstance(result.contents, types.MarkupContent)
    assert result.contents.value.startswith("Perform horizontal regridding.")
    assert result.range == types.Range(
        start=types.Position(line=4, character=4),
        end=types.Position(line=4, character=10),
    )


def test_hover_preprocessor(split_cursor: SplitCursor) -> None:
    text, position = split_cursor(RECIPE.replace(": pp", ": p|p"))
    result = hover(text, position, yaml.safe_load(RECIPE), recipe_schema())
    assert result is not None
    assert isinstance(result.contents, types.MarkupContent)
    assert "target_grid: 1x1" in result.contents.value


def test_hover_value(split_cursor: SplitCursor) -> None:
    text, position = split_cursor(RECIPE.replace("CMIP6", "CM|IP6"))
    result = hover(text, position, MISSING, recipe_schema())
    assert result is not None
    assert isinstance(result.contents, types.MarkupContent)
    assert result.contents.value == "Project, for example CMIP6."


def test_hover_default(split_cursor: SplitCursor) -> None:
    text, position = split_cursor(
        RECIPE.replace(
            "  other:\n",
            "  other:\n    climate_statistics:\n      oper|ator: max\n",
        ),
    )
    result = hover(text, position, MISSING, recipe_schema())
    assert result is not None
    assert isinstance(result.contents, types.MarkupContent)
    assert result.contents.value.endswith("\n\nDefault: `mean`")


def test_hover_text_value(split_cursor: SplitCursor) -> None:
    text, position = split_cursor(RECIPE.replace("Example", "Exa|mple"))
    result = hover(text, position, MISSING, recipe_schema())
    assert result is not None
    assert isinstance(result.contents, types.MarkupContent)
    assert result.contents.value == "Recipe title."


@pytest.fixture
def esmvaltool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Define tags and references in a fake ESMValTool installation."""
    (tmp_path / "config-references.yml").write_text(
        dedent("""\
            authors:
              doe_jane:
                name: Doe, Jane
                institute: Example Institute
                orcid:
            themes:
              phys: physics
            """),
    )
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "doe21.bibtex").write_text(
        "@article{doe21,\n  title = {Example}\n}\n",
    )
    monkeypatch.setattr(references, "esmvaltool_directory", lambda: tmp_path)
    references.tags.cache_clear()
    yield
    references.tags.cache_clear()


TAGS_RECIPE = dedent("""\
    documentation:
      authors:
        - doe_jane
      maintainer: [doe_jane, unknown]
      references:
        - doe21
    diagnostics:
      diag:
        themes:
          - phys
    """)


def _hover_tag(split_cursor: SplitCursor, old: str, new: str) -> str | None:
    text, position = split_cursor(TAGS_RECIPE.replace(old, new))
    result = hover(text, position, MISSING, recipe_schema())
    if result is None:
        return None
    assert isinstance(result.contents, types.MarkupContent)
    return result.contents.value


@pytest.mark.usefixtures("esmvaltool")
def test_hover_author(split_cursor: SplitCursor) -> None:
    value = _hover_tag(split_cursor, "- doe_jane", "- doe|_jane")
    assert value == (
        "Author `doe_jane`:\n\n"
        "- name: Doe, Jane\n"
        "- institute: Example Institute"
    )


@pytest.mark.usefixtures("esmvaltool")
def test_hover_maintainer_in_flow_list(split_cursor: SplitCursor) -> None:
    value = _hover_tag(split_cursor, "[doe_jane", "[doe|_jane")
    assert value is not None
    assert value.startswith("Author `doe_jane`:")


@pytest.mark.usefixtures("esmvaltool")
def test_hover_unknown_author(split_cursor: SplitCursor) -> None:
    value = _hover_tag(split_cursor, "unknown]", "unk|nown]")
    assert value is not None
    assert value.startswith("Maintainers")


@pytest.mark.usefixtures("esmvaltool")
def test_hover_reference(split_cursor: SplitCursor) -> None:
    value = _hover_tag(split_cursor, "doe21", "doe|21")
    assert value == (
        "Reference `doe21`:\n\n"
        "```bibtex\n@article{doe21,\n  title = {Example}\n}\n```"
    )


@pytest.mark.usefixtures("esmvaltool")
def test_hover_theme(split_cursor: SplitCursor) -> None:
    value = _hover_tag(split_cursor, "phys", "ph|ys")
    assert value == "Theme `phys`: physics"


def _definition(split_cursor: SplitCursor, text: str) -> types.Location | None:
    text, position = split_cursor(text)
    return definition(text, position, URI)


def test_definition_preprocessor(split_cursor: SplitCursor) -> None:
    location = _definition(split_cursor, RECIPE.replace(": pp", ": p|p"))
    assert location == types.Location(
        uri=URI,
        range=types.Range(
            start=types.Position(line=3, character=2),
            end=types.Position(line=3, character=4),
        ),
    )


def test_definition_ancestor(split_cursor: SplitCursor) -> None:
    location = _definition(
        split_cursor, RECIPE.replace("diag/tas]", "diag/t|as]")
    )
    assert location is not None
    assert location.range.start == types.Position(line=13, character=6)


def test_definition_ancestor_wildcard(split_cursor: SplitCursor) -> None:
    location = _definition(
        split_cursor, RECIPE.replace("diag/tas]", "diag/|*]")
    )
    assert location is not None
    assert location.range.start == types.Position(line=11, character=2)


def test_definition_script(split_cursor: SplitCursor, tmp_path: Path) -> None:
    script = tmp_path / "diagnostic.py"
    script.write_text("")
    location = _definition(
        split_cursor,
        RECIPE.replace("examples/diagnostic.py", f"{script}|"),
    )
    assert location is not None
    assert location.uri == script.as_uri()


def test_definition_invalid_yaml(split_cursor: SplitCursor) -> None:
    text = RECIPE.replace(": pp", ": p|p") + "invalid: [\n"
    assert _definition(split_cursor, text) is None
