from __future__ import annotations

from textwrap import dedent

import pytest
import yaml

from esmvaltool_language_server.cmor import branding_suffixes
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
