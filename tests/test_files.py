from __future__ import annotations

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
