"""Suggest keys and values while editing a recipe."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from lsprotocol import types

from esmvaltool_language_server import references
from esmvaltool_language_server.context import Context, context_at
from esmvaltool_language_server.schema import (
    RecipeSchema,
    data_at,
    properties,
    values,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

# Keys whose values are only collections, so the value starts on the next
# line.
_COLLECTION_TYPES = frozenset({"array", "object", "null"})


def completions(
    text: str,
    position: types.Position,
    recipe: object,
    schema: RecipeSchema,
) -> list[types.CompletionItem]:
    """Suggest keys and values at a position in a recipe.

    Parameters
    ----------
    text:
        The YAML text of the recipe.
    position:
        The position of the cursor.
    recipe:
        The most recent version of the recipe that could be loaded, used to
        decide which suggestions apply. Use
        :obj:`~esmvaltool_language_server.schema.MISSING` if there is none.
    schema:
        The recipe schema.

    Returns
    -------
    :
        The suggestions.
    """
    context = context_at(text, position.line, position.character)
    if context is None:
        return []
    edit_range = types.Range(
        start=types.Position(line=position.line, character=context.start),
        end=position,
    )
    items: list[types.CompletionItem] = []
    if context.kind in {"key", "item"}:
        items.extend(_key_items(context, recipe, schema, edit_range))
    if context.kind in {"value", "item"}:
        items.extend(_value_items(context, recipe, schema, edit_range))
    return items


def _documentation(text: str | None) -> types.MarkupContent | None:
    if text is None:
        return None
    return types.MarkupContent(kind=types.MarkupKind.Markdown, value=text)


def _key_items(
    context: Context,
    recipe: object,
    schema: RecipeSchema,
    edit_range: types.Range,
) -> Iterable[types.CompletionItem]:
    data = data_at(recipe, context.path)
    existing = set(data) - {context.word} if isinstance(data, dict) else set()
    for key, nodes in properties(schema.at(context.path, recipe)).items():
        if key in existing:
            continue
        types_ = schema.types(nodes)
        collection = bool(types_) and types_ <= _COLLECTION_TYPES
        yield types.CompletionItem(
            label=key,
            kind=types.CompletionItemKind.Property,
            documentation=_documentation(schema.description(nodes)),
            text_edit=types.TextEdit(
                range=edit_range,
                new_text=f"{key}:" if collection else f"{key}: ",
            ),
        )


def _yaml(value: object) -> str:
    return yaml.safe_dump(value).removesuffix("\n").removesuffix("\n...")


def _value_items(
    context: Context,
    recipe: object,
    schema: RecipeSchema,
    edit_range: types.Range,
) -> Iterable[types.CompletionItem]:
    path = context.path
    candidates: list[tuple[str, types.CompletionItemKind, str | None]] = [
        (_yaml(value), types.CompletionItemKind.EnumMember, None)
        for value in values(schema.at(path, recipe))
    ]
    if references.is_preprocessor(path):
        candidates.extend(
            (
                name,
                types.CompletionItemKind.Reference,
                preprocessor_documentation(
                    name,
                    data_at(recipe, ("preprocessors", name)),
                ),
            )
            for name in references.preprocessor_names(recipe)
        )
    elif references.is_ancestor(path):
        candidates.extend(
            (name, types.CompletionItemKind.Reference, None)
            for name in references.task_names(recipe)
        )
    elif references.is_script(path):
        candidates.extend(
            (name, types.CompletionItemKind.File, None)
            for name in references.script_names()
        )
    for label, kind, documentation in candidates:
        yield types.CompletionItem(
            label=label,
            kind=kind,
            documentation=_documentation(documentation),
            text_edit=types.TextEdit(range=edit_range, new_text=label),
        )


def preprocessor_documentation(name: str, preprocessor: object) -> str:
    """Describe a preprocessor by its steps."""
    steps = yaml.safe_dump(preprocessor, sort_keys=False).rstrip()
    return f"Preprocessor `{name}`:\n\n```yaml\n{steps}\n```"
