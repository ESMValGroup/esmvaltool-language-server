from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from esmvalcore.config import CFG

from esmvaltool_language_server import vocabulary
from esmvaltool_language_server.vocabulary import (
    controlled_vocabulary,
    describe,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    controlled_vocabulary.cache_clear()
    yield
    controlled_vocabulary.cache_clear()


@pytest.mark.parametrize(
    ("project", "facet", "value", "expected"),
    [
        ("CMIP6", "activity", "ScenarioMIP", "Scenario Model"),
        ("CMIP6", "institute", "NCAR", "National Center for Atmospheric"),
        ("CMIP6", "grid", "gn", "native grid"),
        ("CMIP6", "frequency", "mon", "monthly"),
        ("CMIP7", "region", "glb", "whole of the Earth"),
        ("CMIP7", "grid", "g100", "1.25 x 0.9 degree"),
    ],
)
def test_describe(project: str, facet: str, value: str, expected: str) -> None:
    description = describe(project, facet, value)
    assert description is not None
    assert description.startswith(f"`{value}`: ")
    assert expected in description


def test_describe_cmip6_model() -> None:
    description = describe("CMIP6", "dataset", "CESM2")
    assert description is not None
    assert description.startswith("`CESM2`: CESM2 (2018):\n\n- aerosol:")
    # Semicolons in parentheses do not separate components.
    assert "- atmos: CAM6 (0.9x1.25 finite volume grid; 288 x 192" in (
        description
    )
    assert description.endswith("\n\nInstitution: NCAR")


def test_describe_cmip7_model() -> None:
    description = describe("CMIP7", "dataset", "ACCESS-ESM1-6")
    assert description is not None
    assert description.startswith("`ACCESS-ESM1-6`:\n\n- aerosol: classic\n")
    assert description.endswith(
        "[Essential Model Documentation](https://wcrp-cmip.github.io/"
        "Essential-Model-Documentation/model/access-esm1-6)",
    )


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        ("CMIP6", "all-forcing simulation of the recent past"),
        ("CMIP7", "Simulation of the climate of the recent past"),
    ],
)
def test_describe_experiment(project: str, expected: str) -> None:
    description = describe(project, "exp", "historical")
    assert description is not None
    assert description.startswith(f"`historical`: {expected}")
    assert description.endswith(
        "\n\nActivity: CMIP\n\nParent experiment: piControl",
    )


def test_describe_ensemble() -> None:
    assert describe("CMIP6", "ensemble", "r2i1p3f4") == (
        "`r2i1p3f4`: Realization 2, initialization 1, physics 3, forcing 4."
    )


@pytest.mark.parametrize(
    ("project", "facet", "value"),
    [
        ("CMIP6", "dataset", "unknown"),
        ("CMIP6", "ensemble", "r1"),
        ("CMIP6", "short_name", "tas"),
        ("CMIP5", "ensemble", "r1i1p1"),
        ("unknown", "dataset", "CESM2"),
    ],
)
def test_describe_unknown(project: str, facet: str, value: str) -> None:
    assert describe(project, facet, value) is None


def test_controlled_vocabulary_invalid_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = [tmp_path / name for name in "abcd"]
    for path in paths:
        path.mkdir()
    (paths[0] / "TEST_CV.json").write_text("invalid")
    (paths[1] / "TEST_CV.json").write_text(json.dumps({"CV": []}))
    (paths[2] / "TEST_CV.json").write_text(
        json.dumps({"CV": {"grid_label": {"gn": "native"}}}),
    )
    (paths[3] / "OTHER_CV.json").write_text(
        json.dumps({"CV": {"grid_label": {"gr": "regridded"}}}),
    )
    monkeypatch.setitem(
        CFG["projects"],
        "TEST",
        {"cmor_table": {"type": "esmvalcore.cmor.table.CMIP6Info"}},
    )
    monkeypatch.setattr(
        vocabulary,
        "get_tables",
        lambda *_: type("Info", (), {"paths": paths})(),
    )
    assert controlled_vocabulary("TEST") == {"grid_label": {"gn": "native"}}


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"source": "Model X"}, "`X`: Model X"),
        ({"source": ""}, None),
    ],
)
def test_describe_model_without_components(
    monkeypatch: pytest.MonkeyPatch,
    entry: dict[str, str],
    expected: str | None,
) -> None:
    monkeypatch.setattr(
        vocabulary,
        "controlled_vocabulary",
        lambda _: {"source_id": {"X": entry}},
    )
    assert describe("TEST", "dataset", "X") == expected


def test_describe_experiment_without_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vocabulary,
        "controlled_vocabulary",
        lambda _: {"experiment_id": {"x": {"activity_id": ["CMIP"]}}},
    )
    assert describe("TEST", "exp", "x") is None


RECIPE = {
    "datasets": [
        {"dataset": "CESM2", "project": "OBS6"},
        {"dataset": "CESM2", "project": "CMIP6"},
    ],
    "diagnostics": {"diag": {"variables": {"tas": {"mip": "Amon"}}}},
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("datasets", 1, "dataset"), "`CESM2`: CESM2 (2018)"),
        # OBS6 uses the CMOR tables, but not the vocabulary, of CMIP6.
        (("datasets", 0, "dataset"), None),
        (("datasets", 1, "unknown"), None),
        ((), None),
        ((0,), None),
    ],
)
def test_facet_documentation(
    path: tuple[str | int, ...],
    expected: str | None,
) -> None:
    description = vocabulary.facet_documentation(path, "CESM2", RECIPE)
    if expected is None:
        assert description is None
    else:
        assert description is not None
        assert description.startswith(expected)
