"""Show documentation for the part of a recipe under the cursor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types

from esmvaltool_language_server import cmor, references
from esmvaltool_language_server.completion import preprocessor_documentation
from esmvaltool_language_server.context import context_at
from esmvaltool_language_server.schema import MISSING, RecipeSchema, data_at

if TYPE_CHECKING:
    from esmvaltool_language_server.context import Path

# How to name a tag of each section in its description.
_TAG_KINDS = {
    "authors": "Author",
    "projects": "Project",
    "realms": "Realm",
    "references": "Reference",
    "themes": "Theme",
}


def hover(
    text: str,
    position: types.Position,
    recipe: object,
    schema: RecipeSchema,
) -> types.Hover | None:
    """Describe the key or value at a position in a recipe.

    Keys and values are described by the recipe schema, the names of
    preprocessors by their steps, and tags such as authors and references by
    their definition in ESMValTool.

    Parameters
    ----------
    text:
        The YAML text of the recipe.
    position:
        The position of the cursor.
    recipe:
        The most recent version of the recipe that could be loaded. Use
        :obj:`~esmvaltool_language_server.schema.MISSING` if there is none.
    schema:
        The recipe schema.

    Returns
    -------
    :
        The description, or :obj:`None` if there is nothing to describe.
    """
    context = context_at(text, position.line, position.character)
    if context is None or not context.word:
        return None
    if context.kind == "key":
        nodes = schema.at((*context.path, context.word), recipe)
        description = schema.documentation(nodes)
    elif references.is_preprocessor(context.path):
        preprocessor = data_at(recipe, ("preprocessors", context.word))
        description = (
            None
            if preprocessor is MISSING
            else preprocessor_documentation(context.word, preprocessor)
        )
    else:
        description = _tag_documentation(context.path, context.word)
        if description is None:
            description = _value_documentation(
                context.path, context.word, recipe, schema
            )
    if description is None:
        return None
    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=description,
        ),
        range=types.Range(
            start=types.Position(line=position.line, character=context.start),
            end=types.Position(line=position.line, character=context.end),
        ),
    )


def _value_documentation(
    path: Path,
    value: str,
    recipe: object,
    schema: RecipeSchema,
) -> str | None:
    """Describe a value by the documentation of its key."""
    description = schema.documentation(schema.at(path, recipe))
    if description is None and path and isinstance(path[-1], int):
        # Items of lists are described by the list.
        description = schema.documentation(schema.at(path[:-1], recipe))
    variable = cmor.branding_suffixes(path, recipe).get(value)
    if variable is not None:
        return (
            variable if description is None else f"{description}\n\n{variable}"
        )
    return description


def _tag_documentation(path: Path, name: str) -> str | None:
    """Describe a tag by its definition in ESMValTool."""
    section = references.tag_section(path)
    if section is None:
        return None
    value = references.tag_value(section, name)
    if value is None:
        return None
    title = f"{_TAG_KINDS[section]} `{name}`"
    if section == "references":
        return f"{title}:\n\n```bibtex\n{value}\n```"
    if isinstance(value, dict):
        lines = [f"- {key}: {item}" for key, item in value.items() if item]
        return "\n".join([f"{title}:", "", *lines])
    return f"{title}: {value}"
