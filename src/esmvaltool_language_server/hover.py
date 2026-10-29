"""Show documentation for the part of a recipe under the cursor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types

from esmvaltool_language_server import references
from esmvaltool_language_server.completion import preprocessor_documentation
from esmvaltool_language_server.context import context_at
from esmvaltool_language_server.schema import MISSING, RecipeSchema, data_at

if TYPE_CHECKING:
    from esmvaltool_language_server.context import Path


def hover(
    text: str,
    position: types.Position,
    recipe: object,
    schema: RecipeSchema,
) -> types.Hover | None:
    """Describe the key or value at a position in a recipe.

    Keys and values are described by the recipe schema and the names of
    preprocessors by their steps.

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
        description = _value_documentation(context.path, recipe, schema)
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
    recipe: object,
    schema: RecipeSchema,
) -> str | None:
    """Describe a value by the documentation of its key."""
    description = schema.documentation(schema.at(path, recipe))
    if description is None and path and isinstance(path[-1], int):
        # Items of lists are described by the list.
        description = schema.documentation(schema.at(path[:-1], recipe))
    return description
