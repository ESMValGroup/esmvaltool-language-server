"""Suggest keys and values while editing a recipe."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from lsprotocol import types

from esmvaltool_language_server import references
from esmvaltool_language_server.context import Context, context_at
from esmvaltool_language_server.schema import (
    MISSING,
    RecipeSchema,
    data_at,
    to_yaml,
    values,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from esmvaltool_language_server.schema import JSONSchema

# Keys whose values are only collections, so the value starts on the next
# line.
_COLLECTION_TYPES = frozenset({"array", "object", "null"})


def completions(
    text: str,
    position: types.Position,
    recipe: object,
    schema: RecipeSchema,
    *,
    snippets: bool = False,
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
    snippets:
        Whether the editor supports snippets. If it does, keys are inserted
        with their default value or the keys that their value requires, as
        placeholders.

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
        items.extend(
            _key_items(context, recipe, schema, edit_range, snippets=snippets),
        )
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
    *,
    snippets: bool,
) -> Iterable[types.CompletionItem]:
    data = data_at(recipe, context.path)
    existing = set(data) - {context.word} if isinstance(data, dict) else set()
    for key, nodes in schema.properties(
        schema.at(context.path, recipe)
    ).items():
        if key in existing:
            continue
        text = _key_snippet(key, nodes, schema, context) if snippets else None
        yield types.CompletionItem(
            label=key,
            kind=types.CompletionItemKind.Property,
            documentation=_documentation(schema.documentation(nodes)),
            insert_text_format=(
                types.InsertTextFormat.PlainText
                if text is None
                else types.InsertTextFormat.Snippet
            ),
            # The snippets contain the indentation of each line.
            insert_text_mode=types.InsertTextMode.AsIs,
            text_edit=types.TextEdit(
                range=edit_range,
                new_text=_key_text(key, nodes, schema)
                if text is None
                else text,
            ),
        )


def _key_text(key: str, nodes: list[JSONSchema], schema: RecipeSchema) -> str:
    types_ = schema.types(nodes)
    collection = bool(types_) and types_ <= _COLLECTION_TYPES
    return f"{key}:" if collection else f"{key}: "


def _placeholder(number: int, value: object) -> str:
    if value is MISSING or isinstance(value, dict | list):
        return f"${{{number}}}"
    text = to_yaml(value)
    for char in "\\$}":
        text = text.replace(char, f"\\{char}")
    return f"${{{number}:{text}}}"


def _key_snippet(
    key: str,
    nodes: list[JSONSchema],
    schema: RecipeSchema,
    context: Context,
) -> str | None:
    """Insert a key with the keys its value requires or its default value."""
    required = [] if context.flow else schema.required(nodes)
    if required:
        children = schema.properties(nodes)
        indent = " " * (context.start + 2)
        lines = [f"{key}:"]
        lines.extend(
            f"{indent}{name}: "
            + _placeholder(i, schema.default(children.get(name, [])))
            for i, name in enumerate(required, start=1)
        )
        return "\n".join(lines)
    default = schema.default(nodes)
    if default is MISSING or isinstance(default, dict | list):
        return None
    return f"{key}: {_placeholder(1, default)}"


def _value_items(
    context: Context,
    recipe: object,
    schema: RecipeSchema,
    edit_range: types.Range,
) -> Iterable[types.CompletionItem]:
    path = context.path
    nodes = schema.at(path, recipe)
    default = schema.default(nodes)
    choices = values(nodes)
    if default is not MISSING and default not in choices:
        choices.insert(0, default)
    candidates: list[tuple[str, types.CompletionItemKind, str | None]] = [
        (
            to_yaml(value),
            types.CompletionItemKind.EnumMember,
            "Default value." if value == default else None,
        )
        for value in choices
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
    default_label = None if default is MISSING else to_yaml(default)
    for label, kind, documentation in candidates:
        yield types.CompletionItem(
            label=label,
            kind=kind,
            documentation=_documentation(documentation),
            preselect=label == default_label or None,
            text_edit=types.TextEdit(range=edit_range, new_text=label),
        )


def preprocessor_documentation(name: str, preprocessor: object) -> str:
    """Describe a preprocessor by its steps."""
    steps = yaml.safe_dump(preprocessor, sort_keys=False).rstrip()
    return f"Preprocessor `{name}`:\n\n```yaml\n{steps}\n```"
