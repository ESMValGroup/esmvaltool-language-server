from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from typing import TYPE_CHECKING

import pytest
import requests
from esmvalcore.config import CFG

from esmvaltool_language_server import esgf, vocabulary
from esmvaltool_language_server.vocabulary import (
    controlled_vocabulary,
    describe,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pytest_mock import MockerFixture


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    controlled_vocabulary.cache_clear()
    yield
    controlled_vocabulary.cache_clear()


@pytest.fixture(autouse=True)
def documentation(
    request: pytest.FixtureRequest,
    mocker: MockerFixture,
) -> Iterator[dict[str, object]]:
    """Mock the Essential Model Documentation, without any models.

    Tests marked as online download the real documentation.
    """
    models: dict[str, object] = {}
    vocabulary._emd_description.cache_clear()
    if request.node.get_closest_marker("online"):
        yield models
        vocabulary._emd_description.cache_clear()
        return

    def get(url: str, **_: object) -> requests.Response:
        model = url.rsplit("/", 1)[-1].removesuffix(".json")
        response = mocker.Mock(spec=requests.Response)
        response.status_code = 200 if model in models else 404
        response.json.return_value = models.get(model)
        return response

    mocker.patch.object(vocabulary.requests, "get", side_effect=get)
    yield models
    vocabulary._emd_description.cache_clear()


@pytest.mark.parametrize(
    ("project", "facet", "value", "expected"),
    [
        ("CMIP6", "activity", "ScenarioMIP", "Scenario Model"),
        ("CMIP6", "institute", "NCAR", "National Center for Atmospheric"),
        ("CMIP6", "grid", "gn", "native grid"),
        ("CMIP6", "frequency", "mon", "monthly"),
        ("CMIP7", "region", "glb", "whole of the Earth"),
        ("CMIP7", "grid", "g100", "1.25 x 0.9 degree"),
        ("CORDEX-CMIP6", "domain", "EUR-12", "Europe"),
        ("CORDEX-CMIP6", "exp", "historical", "all-forcing simulation"),
        ("CORDEX-CMIP6", "dataset", "ALARO1-SFX", "ALARO1-SFX regional"),
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


def test_describe_cordex_driving_model() -> None:
    description = describe("CORDEX-CMIP6", "driver", "ACCESS-CM2")
    assert description is not None
    assert description.startswith("`ACCESS-CM2`: ACCESS-CM2 (2019):\n\n- ")
    assert description.endswith("\n\nInstitution: CSIRO-ARCCSS")


def test_describe_cmip7_model() -> None:
    # Without Essential Model Documentation, the components are described.
    description = describe("CMIP7", "dataset", "ACCESS-ESM1-6")
    assert description is not None
    assert description.startswith("`ACCESS-ESM1-6`:\n\n- aerosol: classic\n")
    assert "Essential Model Documentation" not in description


def test_describe_documented_cmip7_model(
    documentation: dict[str, object],
) -> None:
    documentation["access-esm1-6"] = {"description": "A model."}
    assert describe("CMIP7", "dataset", "ACCESS-ESM1-6") == (
        "`ACCESS-ESM1-6`: A model.\n\n"
        "[Essential Model Documentation of ACCESS-ESM1-6]"
        "(https://wcrp-cmip.github.io/"
        "Essential-Model-Documentation/docs/Data_Users/Model_viewer/"
        "?model=access-esm1-6)"
    )


@pytest.mark.online
def test_describe_cmip7_model_online() -> None:
    # Notice when the Essential Model Documentation changes.
    description = describe("CMIP7", "dataset", "ACCESS-ESM1-6")
    assert description is not None
    assert "- aerosol:" not in description
    assert "[Essential Model Documentation of ACCESS-ESM1-6]" in description


def test_describe_cmip7_model_offline(
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mocker.patch.object(
        vocabulary.requests,
        "get",
        side_effect=requests.ConnectionError("offline"),
    )
    description = describe("CMIP7", "dataset", "ACCESS-ESM1-6")
    assert description is not None
    assert description.startswith("`ACCESS-ESM1-6`:\n\n- aerosol: classic\n")
    assert "Failed to download" in caplog.text


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


def test_describe_configured_facet(monkeypatch: pytest.MonkeyPatch) -> None:
    # The intake-esgf data source in the configuration translates the facet.
    source = {
        "type": "esmvalcore.io.intake_esgf.IntakeESGFDataSource",
        "facets": {"model": "source_id"},
    }
    projects = {"CMIP6": {"data": {"esgf": source}}}
    monkeypatch.setattr(esgf, "CFG", {"projects": projects})
    description = describe("CMIP6", "model", "CESM2")
    assert description is not None
    assert description.startswith("`CESM2`: CESM2 (2018)")


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
    assert describe("CMIP6", "dataset", "X") == expected


def test_describe_experiment_without_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vocabulary,
        "controlled_vocabulary",
        lambda _: {"experiment_id": {"x": {"activity_id": ["CMIP"]}}},
    )
    assert describe("CMIP6", "exp", "x") is None


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


PROJECT_DOCS = "https://docs.esmvaltool.org/en/v2.13.0/input.html"
NATIVE_DOCS = (
    "https://docs.esmvaltool.org/projects/ESMValCore/en/latest/"
    "quickstart/find_data.html"
)


@pytest.fixture
def versions(mocker: MockerFixture) -> Iterator[None]:
    """Pretend that a release of ESMValTool is installed."""
    installed = {"ESMValTool": "2.13.0", "ESMValCore": "2.14.0.dev1"}
    mocker.patch.object(vocabulary, "version", side_effect=installed.get)
    vocabulary._docs_version.cache_clear()
    yield
    vocabulary._docs_version.cache_clear()


@pytest.mark.usefixtures("versions")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "CMIP6",
            "[Project website](https://wcrp-cmip.org/cmip-phases/cmip6/)",
        ),
        ("OBS6", f"[ESMValTool documentation]({PROJECT_DOCS}#observations)"),
        (
            "ICON",
            f"[ESMValCore documentation]({NATIVE_DOCS}#supported-native-models)",
        ),
        ("unknown", None),
    ],
)
def test_facet_documentation_project(value: str, expected: str | None) -> None:
    path = ("datasets", 1, "project")
    description = vocabulary.facet_documentation(path, value, RECIPE)
    if expected is None:
        assert description is None
    else:
        assert description == f"`{value}`: {expected}"


def test_docs_version_not_installed(mocker: MockerFixture) -> None:
    mocker.patch.object(
        vocabulary, "version", side_effect=PackageNotFoundError
    )
    vocabulary._docs_version.cache_clear()
    assert vocabulary._docs_version("ESMValTool") == "latest"
    vocabulary._docs_version.cache_clear()
