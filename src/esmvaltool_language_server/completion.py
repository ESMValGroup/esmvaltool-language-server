"""Suggest keys and values while editing a recipe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml
from lsprotocol import types

from esmvaltool_language_server import cmor, references
from esmvaltool_language_server.context import Context, context_at
from esmvaltool_language_server.schema import (
    MISSING,
    RecipeSchema,
    data_at,
    to_yaml,
    values,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from collections.abc import Set as AbstractSet

    from esmvaltool_language_server.context import Path
    from esmvaltool_language_server.schema import JSONSchema

    FacetValues = Callable[
        [str, Mapping[object, object], str], list[str] | None
    ]
    """Get the available values of a facet from the project, the facets of
    the dataset, and the facet, or None if they are not known yet."""

    UnusedFacets = Callable[[str], AbstractSet[str]]
    """Get the facets that are not used to find the data of a project."""

# Keys whose values are only collections, so the value starts on the next
# line.
_COLLECTION_TYPES = frozenset({"array", "object", "null"})


@dataclass(frozen=True)
class Options:
    """Options for suggesting keys and values.

    Parameters
    ----------
    snippets:
        Whether the editor supports snippets. If it does, keys are inserted
        with their default value or the keys that their value requires, as
        placeholders.
    facet_values:
        Get the values of a facet of the available datasets. If given, these
        are suggested for facets instead of the values that the schema
        allows.
    unused_facets:
        Get the facets that are not used to find the data of a project. If
        given, these are not suggested as keys of datasets of the project.
    """

    snippets: bool = False
    facet_values: FacetValues | None = None
    unused_facets: UnusedFacets | None = None


def completions(
    text: str,
    position: types.Position,
    recipe: object,
    schema: RecipeSchema,
    options: Options | None = None,
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
    options:
        Options for the suggestions.

    Returns
    -------
    :
        The suggestions.
    """
    context = context_at(text, position.line, position.character)
    if context is None:
        return []
    options = options or Options()
    edit_range = types.Range(
        start=types.Position(line=position.line, character=context.start),
        end=position,
    )
    items: list[types.CompletionItem] = []
    if context.kind in {"key", "item"}:
        items.extend(
            _key_items(
                context,
                recipe,
                schema,
                edit_range,
                options,
            ),
        )
    if context.kind in {"value", "item"}:
        items.extend(
            _value_items(
                context,
                recipe,
                schema,
                edit_range,
                options.facet_values,
            ),
        )
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
    options: Options,
) -> Iterable[types.CompletionItem]:
    data = data_at(recipe, context.path)
    existing = set(data) - {context.word} if isinstance(data, dict) else set()
    if options.unused_facets is not None:
        existing |= _unused_facets(
            context.path,
            recipe,
            options.unused_facets,
        )
    for key, nodes in schema.properties(
        schema.at(context.path, recipe)
    ).items():
        if key in existing:
            continue
        text = (
            _key_snippet(key, nodes, schema, context)
            if options.snippets
            else None
        )
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


def _unused_facets(
    path: Path,
    recipe: object,
    unused_facets: UnusedFacets,
) -> set[str]:
    """Get the facets that none of the datasets at a path use."""
    unused: set[str] | None = None
    for facets in cmor.dataset_facets(path, recipe):
        project = facets.get("project")
        if not cmor.is_name(project):
            return set()
        facets_ = set(unused_facets(str(project)))
        unused = facets_ if unused is None else unused & facets_
    return unused or set()


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
    facet_values: FacetValues | None,
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
    if suffixes := cmor.branding_suffixes(path, recipe):
        # The schema allows the suffixes of all variables in the table.
        candidates = [
            (to_yaml(suffix), types.CompletionItemKind.EnumMember, text)
            for suffix, text in suffixes.items()
        ]
    if facet_values is not None and (
        available := _available_values(path, recipe, facet_values)
    ):
        documentation = {label: text for label, _, text in candidates}
        candidates = [
            (
                label,
                types.CompletionItemKind.EnumMember,
                documentation.get(label),
            )
            for label in (
                to_yaml(_typed(value, schema.types(nodes)))
                for value in available
            )
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


def _available_values(
    path: Path,
    recipe: object,
    facet_values: FacetValues,
) -> list[str]:
    """Get the values of the facet at a path of the available datasets."""
    if path and isinstance(path[-1], int):
        # An item in a list of values.
        path = path[:-1]
    if not path or not isinstance(path[-1], str):
        return []
    values: set[str] = set()
    for facets in cmor.dataset_facets(path[:-1], recipe):
        project = facets.get("project")
        if cmor.is_name(project):
            values.update(facet_values(str(project), facets, path[-1]) or [])
    return sorted(values)


def _typed(value: str, allowed: set[str]) -> object:
    """Convert a value to a number if the schema does not allow a string."""
    if "string" in allowed or not allowed & {"integer", "number"}:
        return value
    number = yaml.safe_load(value)
    if isinstance(number, int | float) and not isinstance(number, bool):
        return number
    return value


def preprocessor_documentation(name: str, preprocessor: object) -> str:
    """Describe a preprocessor by its steps."""
    steps = yaml.safe_dump(preprocessor, sort_keys=False).rstrip()
    return f"Preprocessor `{name}`:\n\n```yaml\n{steps}\n```"
