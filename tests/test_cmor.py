from __future__ import annotations

from textwrap import dedent

import pytest
import yaml

from esmvaltool_language_server.cmor import (
    branding_suffixes,
    dataset_facets,
    mips,
)
from esmvaltool_language_server.schema import MISSING

RECIPE = yaml.safe_load(
    dedent("""\
    datasets:
      - {dataset: A, project: CMIP7}
    diagnostics:
      diag:
        additional_datasets:
          - {dataset: B}
        variables:
          tas:
            mip: atmos
          ice:
            short_name: tas
            mip: landIce
            additional_datasets:
              - {dataset: C, project: CMIP6}
          any_mip:
            short_name: tas
            mip: "a*"
    """),
)

TAS_ATMOS = "tavg-h2m-hxy-u"
TAS_LAND_ICE = "tavg-h2m-hxy-is"


@pytest.mark.parametrize(
    ("path", "included", "excluded"),
    [
        (
            ("diagnostics", "diag", "variables", "tas"),
            {TAS_ATMOS},
            {TAS_LAND_ICE, "tavg-u-hxy-u"},
        ),
        (
            ("diagnostics", "diag", "variables", "ice"),
            {TAS_LAND_ICE},
            {TAS_ATMOS},
        ),
        (
            ("diagnostics", "diag", "variables", "any_mip"),
            {TAS_ATMOS},
            {TAS_LAND_ICE},
        ),
        (("datasets", 0), {TAS_ATMOS, TAS_LAND_ICE}, set()),
    ],
)
def test_branding_suffixes(
    path: tuple[str | int, ...],
    included: set[str],
    excluded: set[str],
) -> None:
    suffixes = branding_suffixes((*path, "branding_suffix"), RECIPE)
    assert included <= set(suffixes)
    assert not excluded & set(suffixes)


def test_branding_suffixes_documentation() -> None:
    path = ("diagnostics", "diag", "variables", "tas", "branding_suffix")
    text = branding_suffixes(path, RECIPE)[TAS_ATMOS]
    assert text.startswith(
        "`tas_tavg-h2m-hxy-u` in table `atmos`: Near-Surface Air Temperature",
    )


@pytest.mark.parametrize(
    ("path", "recipe"),
    [
        # No project.
        (("diagnostics", "diag", "additional_datasets", 0), RECIPE),
        # A project without branded variables.
        (
            (
                "diagnostics",
                "diag",
                "variables",
                "ice",
                "additional_datasets",
                0,
            ),
            RECIPE,
        ),
        (("diagnostics", "diag", "variables", "tas"), MISSING),
        (("diagnostics", "diag", "variables", "tas", "mip"), RECIPE),
    ],
)
def test_no_branding_suffixes(
    path: tuple[str | int, ...], recipe: object
) -> None:
    assert branding_suffixes((*path, "branding_suffix"), recipe) == {}


def test_branding_suffixes_unknown_project() -> None:
    recipe = {
        "datasets": [{"dataset": "A", "project": "UNKNOWN"}],
        "diagnostics": {"diag": {"variables": {"tas": {"mip": "atmos"}}}},
    }
    path = ("diagnostics", "diag", "variables", "tas", "branding_suffix")
    assert branding_suffixes(path, recipe) == {}


SUPPLEMENTARY_RECIPE = yaml.safe_load(
    dedent(
        """
        diagnostics:
          diag:
            variables:
              tas:
                mip: Amon
                ensemble: r1i1p1
                supplementary_variables:
                  - {short_name: sftlf, mip: fx}
                  - {short_name: areacella, mip: fx}
                additional_datasets:
                  - project: CMIP5
                    dataset: CanESM2
                    supplementary_variables:
                      - {short_name: sftlf, ensemble: r0i0p0}
        """,
    ),
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            (
                "diagnostics",
                "diag",
                "variables",
                "tas",
                "additional_datasets",
                0,
                "supplementary_variables",
                0,
            ),
            {"short_name": "sftlf", "mip": "fx", "ensemble": "r0i0p0"},
        ),
        (
            (
                "diagnostics",
                "diag",
                "variables",
                "tas",
                "supplementary_variables",
                1,
            ),
            {"short_name": "areacella", "mip": "fx", "ensemble": "r1i1p1"},
        ),
    ],
)
def test_supplementary_facets(
    path: tuple[str | int, ...],
    expected: dict[str, str],
) -> None:
    [facets] = dataset_facets(path, SUPPLEMENTARY_RECIPE)
    assert "supplementary_variables" not in facets
    assert facets["project"] == "CMIP5"
    assert facets["dataset"] == "CanESM2"
    assert {key: facets[key] for key in expected} == expected


def _recipe(**facets: object) -> dict[str, object]:
    """Make a recipe with one variable group that has the given facets."""
    return {
        "diagnostics": {
            "diag": {"variables": {"tas": {"mip": "Amon", **facets}}},
        },
    }


TAS_MIP = ("diagnostics", "diag", "variables", "tas", "mip")


def test_mips_project_in_datasets_section() -> None:
    recipe = {
        "datasets": [{"dataset": "A", "project": "CMIP6"}],
        **_recipe(),
    }
    result = mips(TAS_MIP, recipe)
    assert result["Amon"] == "Frequency: `mon`"
    # Only the tables of CMIP6, not those of CMIP7.
    assert "atmos" not in result


def test_mips_project_in_dataset() -> None:
    recipe = _recipe(
        additional_datasets=[{"dataset": "A", "project": "CMIP7"}]
    )
    result = mips(TAS_MIP, recipe)
    assert "atmos" in result
    assert "Amon" not in result
    path = (*TAS_MIP[:-1], "additional_datasets", 0, "mip")
    assert mips(path, recipe) == result


def test_mips_multiple_projects() -> None:
    recipe = _recipe(
        additional_datasets=[
            {"dataset": "A", "project": "CMIP7"},
            {"dataset": "B", "project": "CMIP6"},
        ],
    )
    assert {"atmos", "Amon"} <= set(mips(TAS_MIP, recipe))


def test_mips_in_list() -> None:
    recipe = _recipe(project="CMIP7", mip=["atmos", ""])
    assert "ocean" in mips((*TAS_MIP, 1), recipe)


@pytest.mark.parametrize(
    "recipe",
    [_recipe(), _recipe(project="UNKNOWN"), _recipe(project="CM*")],
)
def test_mips_unknown_project(recipe: object) -> None:
    # The tables of all projects are included.
    assert {"Amon", "atmos"} <= set(mips(TAS_MIP, recipe))


@pytest.mark.parametrize(
    "path",
    [("diagnostics", "diag", "variables", "tas", "short_name"), ()],
)
def test_no_mips(path: tuple[str | int, ...]) -> None:
    assert mips(path, _recipe(project="CMIP6")) == {}
