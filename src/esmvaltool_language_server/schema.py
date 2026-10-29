"""Look up the parts of the recipe schema that apply at a location."""

from __future__ import annotations

import functools
import re
from typing import TYPE_CHECKING, Any

import jsonschema
from esmvalcore.recipe import generate_recipe_schema

if TYPE_CHECKING:
    from esmvaltool_language_server.context import Path

JSONSchema = dict[str, Any]


class _Missing:
    """Marker for data that is not in the recipe."""


MISSING = _Missing()

# Value of the key at the cursor if the recipe does not have it yet, so the
# rules for that key apply. It is a string without wildcards.
_PLACEHOLDER = "placeholder"


def child(data: object, part: str | int) -> object:
    """Get an item of a mapping or sequence, or :obj:`MISSING`."""
    if isinstance(part, str) and isinstance(data, dict):
        return data.get(part, MISSING)
    if isinstance(part, int) and isinstance(data, list):
        return data[part] if part < len(data) else MISSING
    return MISSING


def data_at(data: object, path: Path) -> object:
    """Get the data at a path, or :obj:`MISSING`."""
    for part in path:
        data = child(data, part)
    return data


class RecipeSchema:
    """The recipe schema."""

    def __init__(self, schema: JSONSchema) -> None:
        self.schema = schema
        self._validator = jsonschema.Draft7Validator(schema)

    def at(self, path: Path, recipe: object) -> list[JSONSchema]:
        """Find the schemas that apply to the data at a path.

        Parameters
        ----------
        path:
            The path in the recipe.
        recipe:
            The recipe, used to decide which conditional rules apply. Use
            :obj:`MISSING` if the recipe is not known.

        Returns
        -------
        :
            The schemas that apply. The data must be valid for all of them,
            except those that are alternatives (``anyOf`` and ``oneOf``).
        """
        nodes = [self.schema]
        data = recipe
        for i, part in enumerate(path):
            if i == len(path) - 1 and isinstance(part, str):
                if data is MISSING:
                    data = {part: _PLACEHOLDER}
                elif isinstance(data, dict) and part not in data:
                    data = {**data, part: _PLACEHOLDER}
            nodes = [
                node
                for schema in self._expand_all(nodes, data)
                for node in _children(schema, part)
            ]
            data = child(data, part)
        return self._expand_all(nodes, data)

    def _expand_all(
        self,
        nodes: list[JSONSchema],
        data: object,
    ) -> list[JSONSchema]:
        return [
            schema for node in nodes for schema in self._expand(node, data)
        ]

    def _expand(self, node: JSONSchema, data: object) -> list[JSONSchema]:
        if not isinstance(node, dict):
            return []
        if "$ref" in node:
            return self._expand(self._resolve(node["$ref"]), data)
        result = [node]
        for keyword in ("allOf", "anyOf", "oneOf"):
            for sub in node.get(keyword, []):
                result.extend(self._expand(sub, data))
        if "if" in node and data is not MISSING:
            valid = self._validator.evolve(schema=node["if"]).is_valid(data)
            branch = node.get("then" if valid else "else")
            if branch is not None:
                result.extend(self._expand(branch, data))
        return result

    def _resolve(self, ref: str) -> JSONSchema:
        node: Any = self.schema
        for part in ref.removeprefix("#/").split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        resolved: JSONSchema = node
        return resolved

    def description(self, nodes: list[JSONSchema]) -> str | None:
        """Get the description of the first schema that has one."""
        for schema in self._expand_all(nodes, MISSING):
            for keyword in ("markdownDescription", "description"):
                if keyword in schema:
                    return str(schema[keyword])
        return None

    def types(self, nodes: list[JSONSchema]) -> set[str]:
        """Get the JSON types that the schemas allow."""
        result: set[str] = set()
        for schema in self._expand_all(nodes, MISSING):
            types = schema.get("type", [])
            result.update([types] if isinstance(types, str) else types)
            if "const" in schema:
                result.add(_json_type(schema["const"]))
        return result


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    return "null" if value is None else "object"


def _children(schema: JSONSchema, part: str | int) -> list[JSONSchema]:
    if isinstance(part, str):
        properties = schema.get("properties", {})
        if part in properties:
            return [properties[part]]
        matches = [
            node
            for pattern, node in schema.get("patternProperties", {}).items()
            if re.search(pattern, part)
        ]
        if matches:
            return matches
        additional = schema.get("additionalProperties")
        return [additional] if isinstance(additional, dict) else []
    items = schema.get("items")
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list) and part < len(items):
        return [items[part]]
    return []


def properties(schemas: list[JSONSchema]) -> dict[str, list[JSONSchema]]:
    """Get the keys that the schemas describe, with their schemas."""
    result: dict[str, list[JSONSchema]] = {}
    for schema in schemas:
        for key, node in schema.get("properties", {}).items():
            result.setdefault(key, []).append(node)
    return result


def values(schemas: list[JSONSchema]) -> list[Any]:
    """Get the values that the schemas list."""
    result: list[Any] = []
    for schema in schemas:
        candidates = list(schema.get("enum", []))
        if "const" in schema:
            candidates.append(schema["const"])
        if schema.get("type") == "boolean":
            candidates.extend([True, False])
        result.extend(value for value in candidates if value not in result)
    return result


@functools.cache
def recipe_schema() -> RecipeSchema:
    """Get the recipe schema for the current configuration."""
    return RecipeSchema(generate_recipe_schema())
