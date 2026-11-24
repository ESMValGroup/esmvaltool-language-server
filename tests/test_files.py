from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from esmvalcore.config import CFG

from esmvaltool_language_server import files

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

FILES = (
    "Tier2/ERA5/OBS6_ERA5_reanaly_v1_Amon_tas_199001-199112.nc",
    "Tier2/ERA5/OBS6_ERA5_reanaly_v1_day_tas_19900101-19911231.nc",
    "Tier2/ERA5/OBS6_ERA5_reanaly_v2_Amon_pr_199001-199112.nc",
    "Tier3/MSWEP/OBS6_MSWEP_reanaly_v1_Amon_pr_199001-199112.nc",
    "Tier3/MSWEP/OBS6_MSWEP_reanaly_v1_Amon_pr_199201-199512.nc",
)


def _clear_caches() -> None:
    files._session.cache_clear()
    files.known_facets.cache_clear()


@pytest.fixture
def local_data(tmp_path: Path) -> Iterator[None]:
    for name in FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    data = {
        "local": {
            "type": "esmvalcore.io.local.LocalDataSource",
            "rootpath": str(tmp_path),
            "dirname_template": "Tier{tier}/{dataset}",
            "filename_template": (
                "{project}_{dataset}_{type}_{version}_{mip}_{short_name}"
                "[_.]*nc"
            ),
        },
        "esgf": {"type": "esmvalcore.io.intake_esgf.IntakeESGFDataSource"},
    }
    projects = {
        **CFG["projects"],
        "OBS6": {**CFG["projects"]["OBS6"], "data": data},
        "CMIP6": {**CFG["projects"]["CMIP6"], "data": {"esgf": data["esgf"]}},
    }
    _clear_caches()
    with CFG.context(projects=projects):
        yield
    _clear_caches()


@pytest.mark.usefixtures("local_data")
def test_known_facets() -> None:
    assert files.known_facets("OBS6") == {
        "dataset",
        "mip",
        "project",
        "short_name",
        "tier",
        "type",
        "version",
    }
    assert files.known_facets("CMIP6") == set()
    assert files.is_searchable("OBS6", "version")
    assert not files.is_searchable("OBS6", "short_name")
    assert not files.is_searchable("OBS6", "exp")
    assert not files.is_searchable("CMIP6", "dataset")
    assert files.is_searchable("OBS6", "timerange")
    assert not files.is_searchable("CMIP6", "timerange")


@pytest.mark.usefixtures("local_data")
@pytest.mark.parametrize(
    ("facets", "facet", "expected"),
    [
        ({"short_name": "pr"}, "dataset", ["ERA5", "MSWEP"]),
        ({"short_name": "pr", "tier": 3}, "dataset", ["MSWEP"]),
        ({"short_name": "pr", "dataset": "M*"}, "dataset", ["ERA5", "MSWEP"]),
        ({"short_name": "tas", "dataset": "ERA5"}, "mip", ["Amon", "day"]),
        ({"short_name": "tas", "mip": "Amon"}, "version", ["v1"]),
        ({"short_name": "tas", "preprocessor": "pp"}, "tier", ["2"]),
        ({"short_name": "ua"}, "dataset", []),
        (
            {"short_name": "pr", "dataset": "MSWEP"},
            "timerange",
            ["*", "1990/1995"],
        ),
        ({"short_name": "pr"}, "timerange", ["*", "1990/1991", "1990/1995"]),
        (
            {"short_name": "pr", "timerange": "2000/2001"},
            "start_year",
            ["1990"],
        ),
        ({"short_name": "pr"}, "end_year", ["1991", "1995"]),
        ({"short_name": "ua"}, "timerange", []),
    ],
)
def test_facet_values(
    facets: dict[object, object],
    facet: str,
    expected: list[str],
) -> None:
    assert files.facet_values("OBS6", facets, facet) == expected


@pytest.mark.usefixtures("local_data")
def test_facet_values_not_searchable() -> None:
    assert files.facet_values("CMIP6", {"short_name": "tas"}, "dataset") == []
    assert files.facet_values("OBS6", {"short_name": "tas"}, "exp") == []


@pytest.mark.usefixtures("local_data")
def test_known_facets_without_searchable_facets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Data sources without searchable facets are deprecated in ESMValCore.
    monkeypatch.setattr(files, "load_data_sources", lambda *_: [object()])
    assert files.known_facets("OBS6") == set()


def test_time_ranges_are_split_at_missing_years() -> None:
    found = SimpleNamespace(
        files=[
            SimpleNamespace(facets={"timerange": "199501/199612"}),
            SimpleNamespace(facets={"timerange": "199001/199112"}),
            SimpleNamespace(facets={}),
        ],
    )
    dataset = SimpleNamespace(from_files=lambda: [found])
    assert list(files._time_ranges(dataset)) == [
        ("199001", "199112"),
        ("199501", "199612"),
    ]


@pytest.mark.usefixtures("local_data")
@pytest.mark.parametrize(
    ("facets", "expected"),
    [
        ({"short_name": "pr"}, [("ERA5", "v2"), ("MSWEP", "v1")]),
        ({"short_name": "pr", "timerange": "1990/1993"}, [("MSWEP", "v1")]),
        (
            {"short_name": "pr", "start_year": 1991},
            [("ERA5", "v2"), ("MSWEP", "v1")],
        ),
        ({"short_name": "ua"}, []),
    ],
)
def test_datasets(
    facets: dict[object, object],
    expected: list[tuple[str, ...]],
) -> None:
    assert files.datasets("OBS6", facets, ("dataset", "version")) == expected


@pytest.mark.usefixtures("local_data")
def test_datasets_not_searchable() -> None:
    facets = {"short_name": "pr"}
    assert files.datasets("OBS6", facets, ("dataset", "exp")) == []
    assert files.datasets("CMIP6", facets, ("dataset",)) == []


# The model, experiment, and dates of the files of CMIP6. Data of NorESM2-LM
# is missing for 2013 and 2014.
CMIP6_FILES = (
    ("CESM2", "historical", "185001-201412"),
    ("CESM2", "ssp585", "201501-210012"),
    ("MIROC6", "historical", "185001-201412"),
    ("NorESM2-LM", "historical", "185001-201212"),
    ("NorESM2-LM", "ssp585", "201501-210012"),
)


@pytest.fixture
def cmip6_data(tmp_path: Path) -> Iterator[None]:
    for model, exp, dates in CMIP6_FILES:
        name = f"tas_Amon_{model}_{exp}_r1i1p1f1_gn_{dates}.nc"
        path = tmp_path / model / exp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    data = {
        "local": {
            "type": "esmvalcore.io.local.LocalDataSource",
            "rootpath": str(tmp_path),
            "dirname_template": "{dataset}/{exp}",
            "filename_template": (
                "{short_name}_{mip}_{dataset}_{exp}_{ensemble}_{grid}*.nc"
            ),
        },
    }
    projects = {
        **CFG["projects"],
        "CMIP6": {**CFG["projects"]["CMIP6"], "data": data},
    }
    _clear_caches()
    with CFG.context(projects=projects):
        yield
    _clear_caches()


@pytest.mark.usefixtures("cmip6_data")
@pytest.mark.parametrize(
    ("facets", "expected"),
    [
        ({"exp": "historical"}, ["CESM2", "MIROC6", "NorESM2-LM"]),
        # Each experiment of a list is needed.
        ({"exp": ["historical", "ssp585"]}, ["CESM2", "NorESM2-LM"]),
        (
            {"exp": ["historical", "ssp585"], "timerange": "1990/2050"},
            ["CESM2"],
        ),
    ],
)
def test_datasets_of_experiments(
    facets: dict[object, object],
    expected: list[str],
) -> None:
    found = files.datasets(
        "CMIP6",
        {"short_name": "tas", "mip": "Amon", **facets},
        ("dataset",),
    )
    assert found == [(name,) for name in expected]
