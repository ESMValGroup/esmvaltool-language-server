"""Show documentation for the part of a recipe under the cursor."""

from __future__ import annotations

from lsprotocol import types

from esmvaltool_language_server import references
from esmvaltool_language_server.completion import preprocessor_documentation
from esmvaltool_language_server.context import context_at
from esmvaltool_language_server.schema import MISSING, RecipeSchema, data_at


def hover(
    text: str,
    position: types.Position,
    recipe: object,
    schema: RecipeSchema,
) -> types.Hover | None:
    """Describe the key or value at a position in a recipe.

    Keys are described by the recipe schema and the names of preprocessors
    by their steps.

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
        description = schema.description(nodes)
    elif references.is_preprocessor(context.path):
        preprocessor = data_at(recipe, ("preprocessors", context.word))
        description = (
            None
            if preprocessor is MISSING
            else preprocessor_documentation(context.word, preprocessor)
        )
    else:
        description = None
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
