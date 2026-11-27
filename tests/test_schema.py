from __future__ import annotations

import pytest

from esmvaltool_language_server.schema import (
    MISSING,
    RecipeSchema,
    data_at,
    properties,
    recipe_schema,
)


def test_step_properties() -> None:
    schema = recipe_schema()
    nodes = schema.at(("preprocessors", "pp", "extract_region"), MISSING)
    assert list(properties(nodes)) == [
        "start_longitude",
        "end_longitude",
        "start_latitude",
        "end_latitude",
    ]


def test_step_description() -> None:
    schema = recipe_schema()
    nodes = schema.at(("preprocessors", "pp", "regrid"), MISSING)
    description = schema.description(nodes)
    assert description is not None
    assert "](https://docs.esmvaltool.org/" in description


def test_data_at() -> None:
    recipe = {"datasets": [{"dataset": "A"}]}
    assert data_at(recipe, ("datasets", 0, "dataset")) == "A"
    assert data_at(recipe, ("datasets", 1)) is MISSING
    assert data_at(recipe, ("datasets", "x")) is MISSING


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"type": "string"}, True),
        ({"type": ["integer", "string"]}, True),
        ({"type": "integer"}, False),
        # Only some strings, as for the tier.
        (
            {
                "anyOf": [
                    {"type": "integer"},
                    {"type": "string", "enum": ["1", "2", "3"]},
                ],
            },
            False,
        ),
    ],
)
def test_allows_any_string(
    value: dict[str, object], *, expected: bool
) -> None:
    schema = RecipeSchema({"type": "object", "properties": {"key": value}})
    nodes = schema.at(("key",), MISSING)
    assert schema.allows_any_string(nodes) is expected
