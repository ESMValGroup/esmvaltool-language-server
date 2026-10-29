from __future__ import annotations

from esmvaltool_language_server.schema import (
    MISSING,
    data_at,
    properties,
    recipe_schema,
    values,
)


def test_mip_values_of_project() -> None:
    schema = recipe_schema()
    path = ("datasets", 0, "mip")
    recipe = {"datasets": [{"dataset": "CanESM5", "project": "CMIP6"}]}
    cmip6 = values(schema.at(path, recipe))
    any_project = values(schema.at(path, MISSING))
    assert "Amon" in cmip6
    assert set(cmip6) < set(any_project)


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
    assert "[recipe documentation]" in description


def test_data_at() -> None:
    recipe = {"datasets": [{"dataset": "A"}]}
    assert data_at(recipe, ("datasets", 0, "dataset")) == "A"
    assert data_at(recipe, ("datasets", 1)) is MISSING
    assert data_at(recipe, ("datasets", "x")) is MISSING
