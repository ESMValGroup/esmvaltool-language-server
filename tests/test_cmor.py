from __future__ import annotations

from textwrap import dedent

import pytest
import yaml

from esmvaltool_language_server.cmor import (
    branding_suffixes,
    dataset_facets,
    mips,
    required_variables,
    table_values,
    variables,
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
        # A project without the table.
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
        # A variable without a branding suffix.
        (
            ("diagnostics", "diag", "variables", "tas"),
            {
                "diagnostics": {
                    "diag": {
                        "variables": {
                            "tas": {"project": "CMIP6", "mip": "Amon"},
                        },
                    },
                },
            },
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
    # Newer versions of ESMValCore also give the realm.
    assert result["Amon"].startswith("Frequency: `mon`")
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
    assert "landIce" in mips((*TAS_MIP, 1), recipe)


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


@pytest.mark.parametrize(
    ("facets", "expected"),
    [
        # Only the tables that contain the variable.
        ({"project": "CMIP7"}, {"atmos", "landIce"}),
        ({"project": "CMIP6", "frequency": "day"}, {"day"}),
        # CMIP7 tables do not define the frequency.
        ({"project": "CMIP7", "frequency": "day"}, {"atmos", "landIce"}),
        (
            {"project": "CMIP7", "branding_suffix": TAS_LAND_ICE},
            {"landIce"},
        ),
        # An alternative name of a variable in the table.
        ({"project": "CMIP6", "short_name": "sic"}, {"SImon", "SIday"}),
    ],
)
def test_mips_of_variable(
    facets: dict[str, object],
    expected: set[str],
) -> None:
    assert set(mips(TAS_MIP, _recipe(**facets))) == expected


@pytest.mark.parametrize(
    ("facets", "reason"),
    [
        ({"project": "native6"}, "are not strict"),
        ({"project": "CMIP6", "derive": True}, "is derived"),
    ],
)
def test_mips_of_variable_in_any_table(
    facets: dict[str, object],
    reason: str,
) -> None:
    result = mips(TAS_MIP, _recipe(**facets))
    assert "Omon" in result
    assert "not in a CMOR table with this MIP" in result["Omon"]
    assert reason in result["Omon"]
    assert result["Amon"].startswith("Frequency: `mon`")
    assert "not in a CMOR table" not in result["Amon"]


def test_mips_of_unknown_variable() -> None:
    result = mips(TAS_MIP, _recipe(project="CMIP6", short_name="unknown"))
    assert {"Amon", "Omon"} <= set(result)


def test_variables() -> None:
    table = variables("CMIP7")
    row = table[
        (table["short_name"] == "tas")
        & (table["branding_suffix"] == TAS_ATMOS)
    ]
    assert set(row["mip"]) == {"atmos"}
    # The frequencies of the controlled vocabulary, except fx.
    assert "mon" in set(row["frequency"])
    assert "fx" not in set(row["frequency"])
    assert variables("UNKNOWN").empty


def _values(facet: str, **facets: object) -> dict[str, str]:
    path = ("diagnostics", "diag", "variables", "tas", facet)
    return table_values(
        path, {"diagnostics": _recipe(**facets)["diagnostics"]}
    )


@pytest.mark.parametrize(
    ("facet", "facets", "expected"),
    [
        # No project.
        ("frequency", {}, set()),
        ("frequency", {"project": "CMIP6"}, {"mon"}),
        (
            "frequency",
            {"project": "CMIP6", "mip": "*"},
            {"1hr", "3hrPt", "6hr", "6hrPt", "day", "mon", "subhrPt"},
        ),
        # CMIP7 tables do not define the frequency.
        (
            "frequency",
            {"project": "CMIP7", "mip": "atmos"},
            {"1hr", "3hr", "6hr", "day", "dec", "mon", "subhr", "yr"},
        ),
        # A variable without a time dimension.
        (
            "frequency",
            {
                "project": "CMIP7",
                "mip": "land",
                "short_name": "orog",
                "branding_suffix": "ti-u-hxy-u",
            },
            {"fx"},
        ),
    ],
)
def test_table_values(
    facet: str,
    facets: dict[str, object],
    expected: set[str],
) -> None:
    result = _values(facet, **facets)
    assert set(result) == expected
    assert not any(result.values())


def test_table_values_in_any_table() -> None:
    # The frequency comes from the requested table, like in ESMValCore.
    result = _values("frequency", project="native6")
    assert result["mon"] == ""
    assert "are not strict" in result["monC"]
    assert "day" not in result


@pytest.mark.parametrize(
    "path",
    [
        ("diagnostics", "diag", "variables", "tas", "mip"),
        ("diagnostics", "diag", "variables", "tas", "frequency", 0),
        ("diagnostics", "diag", "variables", "tas", "realm"),
    ],
)
def test_no_table_values(path: tuple[str | int, ...]) -> None:
    recipe = _recipe(project="CMIP6")
    expected = {"mon": ""} if path[-1] == 0 else {}
    assert table_values(path, recipe) == expected


@pytest.mark.parametrize(
    ("project", "mip"),
    [("CMIP5", "Lmon"), ("CMIP6", "Emon")],
)
def test_required_variables(project: str, mip: str) -> None:
    facets: dict[object, object] = {
        "short_name": "ctotal",
        "project": project,
        "derive": True,
    }
    assert required_variables(facets) == [
        {"short_name": "cVeg", "mip": "Lmon"},
        {"short_name": "cSoil", "mip": mip},
    ]


@pytest.mark.parametrize(
    "facets",
    [
        {"short_name": "tas", "project": "CMIP6"},
        {"short_name": "ctotal", "project": "CMIP7"},
        {"short_name": "ctotal"},
    ],
)
def test_required_variables_not_available(
    facets: dict[object, object],
) -> None:
    with pytest.raises(NotImplementedError):
        required_variables(facets)
