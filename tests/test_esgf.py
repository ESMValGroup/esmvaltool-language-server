from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
import requests
from intake_esgf.core.globus import GlobusESGFIndex
from intake_esgf.core.solr import SolrESGFIndex
from intake_esgf.core.stac import STACESGFIndex

from esmvaltool_language_server import esgf

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Response:
    def __init__(self, data: object, status: int = 200) -> None:
        self.data = data
        self.status = status

    def raise_for_status(self) -> None:
        if self.status != 200:
            msg = f"{self.status} error"
            raise requests.HTTPError(msg)

    def json(self) -> object:
        return self.data


class _Session:
    """Answer requests with the response for the URL."""

    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.params: list[Any] = []

    def get(self, url: str, params: Any = None, **_: object) -> _Response:  # noqa: ANN401
        self.params.append(params)
        return self.responses[url]


class _GlobusClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.data: dict[str, Any] = {}

    def post_search(self, _index_id: str, data: dict[str, Any]) -> object:
        self.data = data
        return self.response


def _index[T](cls: type[T], **attributes: object) -> T:
    """Create an index without connecting to it."""
    index = object.__new__(cls)
    for name, value in attributes.items():
        setattr(index, name, value)
    return index


def test_intake_esgf_mapping() -> None:
    mapping = esgf.intake_esgf_mapping("CMIP5")
    assert mapping is not None
    query = mapping.query(
        {
            "dataset": "ACCESS1-0",
            "exp": ["historical", "rcp85"],
            "ensemble": "r*",
            "mip": "Amon",
            "preprocessor": "pp",
        },
        "mip",
    )
    assert query == {
        "model": ["ACCESS1.0"],
        "experiment": ["historical", "rcp85"],
    }
    assert mapping.value("dataset", "ACCESS1.0") == "ACCESS1-0"
    assert mapping.value("dataset", "CanESM2") == "CanESM2"


def test_configured_intake_esgf_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        "local": {"type": "esmvalcore.local.LocalDataSource"},
        "esgf": {
            "type": "esmvalcore.io.intake_esgf.IntakeESGFDataSource",
            "facets": {"dataset": "source_id"},
        },
    }
    projects = {"CMIP6": {"data": data}}
    monkeypatch.setattr(esgf, "CFG", {"projects": projects})
    mapping = esgf.intake_esgf_mapping("CMIP6")
    assert mapping is not None
    assert mapping.query({"dataset": "CESM2"}, "exp") == {
        "source_id": ["CESM2"],
    }


@pytest.mark.parametrize(
    "mapping",
    [esgf.intake_esgf_mapping("CMIP6"), esgf._solr_mapping("CMIP6")],
)
def test_version_mapping(mapping: esgf._Mapping | None) -> None:
    assert mapping is not None
    query = mapping.query({"version": "v20190429"}, "dataset")
    assert query == {"version": ["20190429"]}
    assert mapping.value("version", "20190429") == "v20190429"


def test_unknown_project_mapping() -> None:
    assert esgf.intake_esgf_mapping("OBS6") is None
    assert esgf._solr_mapping("OBS6") is None


@pytest.mark.parametrize("project", ["CMIP7", "CORDEX-CMIP6"])
def test_project_not_on_solr(project: str) -> None:
    assert esgf._solr_mapping(project) is None


def test_solr_mapping() -> None:
    mapping = esgf._solr_mapping("CMIP6")
    assert mapping is not None
    query = mapping.query({"project": "CMIP6", "short_name": "tas"}, "exp")
    assert query == {"project": ["CMIP6"], "variable": ["tas"]}


def test_globus_values() -> None:
    client = _GlobusClient(
        {"facet_results": [{"buckets": [{"value": "A", "count": 2}]}]},
    )
    index = _index(GlobusESGFIndex, client=client, index_id="id")
    values = esgf._globus_values(index, {"variable_id": ["tas"]}, "source_id")
    assert values == {"A": 2}
    assert client.data["limit"] == 0
    assert {
        "type": "match_any",
        "field_name": "variable_id",
        "values": ["tas"],
    } in client.data["filters"]
    assert client.data["facets"][0]["field_name"] == "source_id"


def test_solr_values() -> None:
    url = "https://node"
    response = {"facet_counts": {"facet_fields": {"model": ["A", 1, "B", 3]}}}
    session = _Session({f"{url}/esg-search/search": _Response(response)})
    index = _index(SolrESGFIndex, session=session, url=url, distrib=False)
    values = esgf._solr_values(
        index,
        {"experiment": ["historical", "rcp85"]},
        "model",
    )
    assert values == {"A": 1, "B": 3}
    params = session.params[0]
    assert ("experiment", "historical") in params
    assert ("experiment", "rcp85") in params
    assert ("facets", "model") in params
    assert ("distrib", "false") in params


@pytest.fixture
def stac_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Session]:
    base = "https://stac/collections"
    session = _Session(
        {
            base: _Response({"collections": [{"id": "CMIP6"}]}),
            f"{base}/CMIP6/queryables": _Response(
                {
                    "properties": {
                        "cmip6:source_id": {},
                        "cmip6:variable_id": {},
                        "latest": {},
                    },
                },
            ),
            f"{base}/CMIP6/aggregations": _Response(
                {"aggregations": [{"name": "cmip6_source_id_frequency"}]},
            ),
            f"{base}/CMIP6/aggregate": _Response(
                {
                    "aggregations": [
                        {"buckets": [{"key": "A", "frequency": 4}]}
                    ]
                },
            ),
        },
    )
    monkeypatch.setattr(
        esgf.intake_esgf.conf,
        "get_cached_session",
        lambda: session,
    )
    esgf._stac_collection.cache_clear()
    yield session
    esgf._stac_collection.cache_clear()


def test_stac_values(stac_session: _Session) -> None:
    index = _index(STACESGFIndex, url="stac", session=stac_session)
    query = {
        "project": ["CMIP6"],
        "variable_id": ["tas", "t'as"],
        "member_id": ["r1i1p1f1"],
    }
    assert esgf._stac_values(index, query, "source_id") == {"A": 4}
    params = stac_session.params[-1]
    assert params["aggregations"] == "cmip6_source_id_frequency"
    # Facets that cannot be searched for are left out.
    assert params["filter"] == (
        "\"cmip6:variable_id\" IN ('tas', 't''as') AND latest=true"
    )


def test_stac_values_without_aggregation(stac_session: _Session) -> None:
    index = _index(STACESGFIndex, url="stac", session=stac_session)
    query = {"project": ["CMIP6"]}
    assert esgf._stac_values(index, query, "member_id") == {}
    assert esgf._stac_values(index, {"project": ["CMIP5"]}, "model") == {}


def test_stac_collection_without_aggregations(
    stac_session: _Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url = "https://stac/collections/CMIP6/aggregations"
    stac_session.responses[url] = _Response("error", 500)
    collection = esgf._stac_collection("stac", "cmip6")
    assert collection is not None
    assert not collection.aggregations
    assert "Cannot count the values of facets" in caplog.text


def test_index_values_error(caplog: pytest.LogCaptureFixture) -> None:
    url = "https://node"
    session = _Session({f"{url}/esg-search/search": _Response("error", 500)})
    index = _index(
        SolrESGFIndex,
        session=session,
        url=url,
        distrib=False,
        repr="SolrESGFIndex('node')",
    )
    with caplog.at_level(logging.WARNING):
        assert esgf._index_values(index, {}, "model") == {}
    assert "Failed to get the values of model" in caplog.text


@pytest.fixture
def indices(monkeypatch: pytest.MonkeyPatch) -> list[esgf.Index]:
    """Use a Globus and a Solr index that return the queries they get."""
    indices = [_index(GlobusESGFIndex), _index(SolrESGFIndex)]
    monkeypatch.setattr(esgf, "_indices", lambda: indices)

    def index_values(
        index: esgf.Index,
        query: esgf.Query,
        field: str,
    ) -> dict[str, int]:
        if isinstance(index, SolrESGFIndex):
            return {"BCC-CSM1.1": 1, f"solr {field} {query}": 1}
        return {f"globus {field} {query}": 1}

    monkeypatch.setattr(esgf, "_index_values", index_values)
    return indices


def test_facet_values(indices: list[esgf.Index]) -> None:
    values = esgf.facet_values("CMIP5", {"exp": "historical"}, "dataset")
    assert values == [
        "bcc-csm1-1",
        "globus model {'experiment': ['historical'], 'project': ['CMIP5']}",
        "solr model {'project': ['CMIP5'], 'experiment': ['historical']}",
    ]


def test_is_searchable(indices: list[esgf.Index]) -> None:
    assert esgf.is_searchable("CMIP6", "dataset")
    assert not esgf.is_searchable("CMIP6", "project")
    assert not esgf.is_searchable("CMIP6", "preprocessor")
    assert not esgf.is_searchable("OBS6", "dataset")


def test_indices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(esgf.intake_esgf.conf, "globus_indices", {})
    monkeypatch.setitem(
        esgf.intake_esgf.conf,
        "solr_indices",
        {"a.org": True, "b.org": False},
    )
    monkeypatch.setitem(esgf.intake_esgf.conf, "stac_indices", {"c.org": True})
    esgf._indices.cache_clear()
    try:
        indices = esgf._indices()
    finally:
        esgf._indices.cache_clear()
    assert [str(index) for index in indices] == [
        "SolrESGFIndex('a.org')",
        "STACESGFIndex('c.org')",
    ]


@pytest.mark.parametrize(
    ("cls", "function"),
    [
        (GlobusESGFIndex, "_globus_values"),
        (SolrESGFIndex, "_solr_values"),
        (STACESGFIndex, "_stac_values"),
    ],
)
def test_index_values(
    monkeypatch: pytest.MonkeyPatch,
    cls: type[esgf.Index],
    function: str,
) -> None:
    monkeypatch.setattr(esgf, function, lambda *_: {"A": 1})
    assert esgf._index_values(_index(cls), {}, "model") == {"A": 1}


@pytest.fixture
def solr_dates(monkeypatch: pytest.MonkeyPatch) -> list[esgf.Query]:
    """Use a Solr index with the first and last dates of datasets."""
    indices = [_index(SolrESGFIndex)]
    monkeypatch.setattr(esgf, "_indices", lambda: indices)
    queries: list[esgf.Query] = []
    counts = {
        "datetime_start": {
            "1848-10-24T12:00:00Z": 2,
            "1850-01-15T12:00:00Z": 31,
        },
        "datetime_stop": {
            "1999-12-15T12:00:00Z": 1,
            "2014-12-15T12:00:00Z": 30,
        },
    }

    def index_values(
        _index: esgf.Index,
        query: esgf.Query,
        field: str,
    ) -> dict[str, int]:
        queries.append(query)
        return counts[field]

    monkeypatch.setattr(esgf, "_index_values", index_values)
    return queries


@pytest.mark.parametrize(
    ("facet", "expected"),
    [
        ("timerange", ["*", "1850/2014"]),
        ("start_year", ["1850"]),
        ("end_year", ["2014"]),
    ],
)
def test_time_values(
    solr_dates: list[esgf.Query],
    facet: str,
    expected: list[str],
) -> None:
    facets = {"dataset": "CESM2", "exp": "historical", "timerange": "*"}
    assert esgf.is_searchable("CMIP6", facet)
    assert esgf.facet_values("CMIP6", facets, facet) == expected
    assert solr_dates[0] == {
        "project": ["CMIP6"],
        "source_id": ["CESM2"],
        "experiment_id": ["historical"],
    }


def test_time_values_not_found(
    monkeypatch: pytest.MonkeyPatch,
    solr_dates: list[esgf.Query],
) -> None:
    monkeypatch.setattr(esgf, "_index_values", lambda *_: {})
    assert esgf.facet_values("CMIP6", {"exp": "historical"}, "timerange") == []
    assert not solr_dates


@pytest.mark.parametrize("project", ["OBS6", "CMIP7"])
def test_time_values_unknown_project(
    project: str,
    solr_dates: list[esgf.Query],
) -> None:
    assert not esgf.is_searchable(project, "timerange")
    assert esgf.facet_values(project, {}, "timerange") == []
    assert not solr_dates


def _item(exp: str, member: str, *dates: str) -> dict[str, Any]:
    """Create a STAC item of a dataset with a file for each time range."""
    activity = "CMIP" if exp == "historical" else "ScenarioMIP"
    names = [f"tas_Amon_CESM2_{exp}_{member}_gn_{d}.nc" for d in dates]
    assets = {
        f"{host}{i}": {"href": f"https://{host}/data/{name}"}
        for i, name in enumerate(names)
        for host in ("a.org", "b.org")
    }
    assets["kerchunk"] = {"href": "https://a.org/dataset.parq"}
    return {
        "id": f"CMIP6.{activity}.NCAR.CESM2.{exp}.{member}.Amon.tas.gn.v1",
        "assets": assets,
        "properties": {
            "cmip6:experiment_id": exp,
            "cmip6:activity_id": [activity],
        },
    }


@pytest.fixture
def stac_dates(
    monkeypatch: pytest.MonkeyPatch,
    stac_session: _Session,
) -> _Session:
    """Use a STAC index with the files of datasets."""
    queryables = stac_session.responses[
        "https://stac/collections/CMIP6/queryables"
    ]
    properties = queryables.data["properties"]  # type: ignore[index]
    for name in ("experiment_id", "activity_id"):
        properties[f"cmip6:{name}"] = {}
    stac_session.responses["https://stac/search"] = _Response(
        {
            "features": [
                _item(
                    "historical",
                    "r1i1p1f1",
                    "185001-194912",
                    "195001-201412",
                ),
                _item("historical", "r2i1p1f1", "18500301-19991130"),
                # A year without data.
                _item(
                    "historical", "r3i1p1f1", "185001-189912", "190101-201412"
                ),
                # No dates.
                _item("historical", "r4i1p1f1", ""),
                # A climatology.
                _item("historical", "r5i1p1f1", "190001-190912-clim"),
            ],
        },
    )
    index = _index(STACESGFIndex, url="stac", session=stac_session)
    monkeypatch.setattr(esgf, "_indices", lambda: [index])
    return stac_session


@pytest.mark.parametrize(
    ("facet", "expected"),
    [
        (
            "timerange",
            [
                "*",
                "1850/1899",
                "1850/2014",
                "185003/199911",
                "1900/1909",
                "1901/2014",
            ],
        ),
        ("start_year", ["1850", "1900", "1901"]),
        ("end_year", ["1899", "1909", "1999", "2014"]),
    ],
)
def test_stac_time_values(
    stac_dates: _Session,
    facet: str,
    expected: list[str],
) -> None:
    facets = {
        "dataset": "CESM2",
        "exp": "historical",
        "short_name": "tas",
        "timerange": "*",
    }
    assert esgf.is_searchable("CMIP6", facet)
    assert esgf.facet_values("CMIP6", facets, facet) == expected
    params = stac_dates.params[-1]
    assert params["collections"] == "CMIP6"
    assert params["fields"] == (
        "id,assets,properties.cmip6:experiment_id,properties.cmip6:activity_id"
    )
    assert params["filter"] == (
        "\"cmip6:source_id\" IN ('CESM2') AND "
        "\"cmip6:experiment_id\" IN ('historical') AND "
        "\"cmip6:variable_id\" IN ('tas') AND latest=true"
    )


def test_stac_time_values_error(
    stac_dates: _Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stac_dates.responses["https://stac/search"] = _Response("error", 500)
    facets = {"exp": "historical"}
    assert esgf.facet_values("CMIP6", facets, "timerange") == []
    assert "Failed to get the dates from STACESGFIndex('stac')" in caplog.text


def test_stac_time_values_unknown_project(stac_dates: _Session) -> None:
    assert esgf.facet_values("CMIP5", {}, "timerange") == []
    assert not esgf.is_searchable("OBS6", "timerange")


@pytest.mark.parametrize("exp", [None, "hist*"])
def test_stac_time_values_without_experiment(
    stac_dates: _Session,
    exp: str | None,
) -> None:
    facets = {"dataset": "CESM2", "exp": exp}
    assert esgf.facet_values("CMIP6", facets, "timerange") == []
    assert not stac_dates.params


def test_solr_time_values_without_experiment(
    solr_dates: list[esgf.Query],
) -> None:
    assert esgf.facet_values("CMIP6", {"dataset": "CESM2"}, "timerange") == []
    assert not solr_dates


def test_stac_time_values_of_experiments(stac_dates: _Session) -> None:
    stac_dates.responses["https://stac/search"] = _Response(
        {
            "features": [
                _item("historical", "r1i1p1f1", "185001-201412"),
                _item("ssp585", "r1i1p1f1", "201501-210012"),
                _item("historical", "r2i1p1f1", "185001-201412"),
                _item("historical", "r3i1p1f1", "185001-201412"),
                _item("ssp585", "r3i1p1f1", "201601-210012"),
            ],
        },
    )
    facets = {"dataset": "CESM2", "exp": ["historical", "ssp585"]}
    assert esgf.facet_values("CMIP6", facets, "timerange") == [
        "*",
        "1850/2014",
        "1850/2100",
        "2016/2100",
    ]


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        ([], []),
        ([("1850", "2014"), ("2015", "2100")], [("1850", "2100")]),
        ([("185001", "201412"), ("201406", "210012")], [("185001", "210012")]),
        ([("1850", "2100"), ("1900", "1950")], [("1850", "2100")]),
        (
            [("2016", "2100"), ("1850", "2014")],
            [("1850", "2014"), ("2016", "2100")],
        ),
    ],
)
def test_join(
    ranges: list[tuple[str, str]],
    expected: list[tuple[str, str]],
) -> None:
    assert esgf._join(ranges) == expected


def test_solr_time_values_of_experiments(
    monkeypatch: pytest.MonkeyPatch,
    solr_dates: list[esgf.Query],
) -> None:
    dates = {
        "historical": ("1850-01-15T12:00:00Z", "2014-12-15T12:00:00Z"),
        "ssp585": ("2015-01-15T12:00:00Z", "2100-12-15T12:00:00Z"),
    }

    def index_values(
        _index: esgf.Index,
        query: esgf.Query,
        field: str,
    ) -> dict[str, int]:
        solr_dates.append(query)
        (exp,) = query["experiment_id"]
        return {dates[exp][field == "datetime_stop"]: 1}

    monkeypatch.setattr(esgf, "_index_values", index_values)
    facets = {"dataset": "CESM2", "exp": ["historical", "ssp585"]}
    assert esgf.facet_values("CMIP6", facets, "timerange") == [
        "*",
        "1850/2100",
    ]
    assert len(solr_dates) == 4


class _GlobusFiles(_GlobusClient):
    """Answer with the number of files with each name and the dataset dates."""

    def __init__(
        self,
        names: dict[str, int],
        dates: dict[str, dict[str, int]] | None = None,
    ) -> None:
        super().__init__({})
        self.values = {"title": names, **(dates or {})}
        self.requests: list[dict[str, Any]] = []

    def post_search(self, _index_id: str, data: dict[str, Any]) -> object:
        self.requests.append(data)
        values = self.values.get(data["facets"][0]["field_name"], {})
        buckets = [{"value": v, "count": c} for v, c in values.items()]
        return {"facet_results": [{"buckets": buckets}]}


def _use_globus(
    monkeypatch: pytest.MonkeyPatch,
    client: _GlobusClient,
) -> None:
    index = _index(
        GlobusESGFIndex,
        client=client,
        index_id="id",
        repr="globus",
    )
    monkeypatch.setattr(esgf, "_indices", lambda: [index])


def test_globus_time_values(monkeypatch: pytest.MonkeyPatch) -> None:
    template = "tas_Amon_CESM2_{exp}_{member}_gn_{dates}.nc"
    client = _GlobusFiles(
        {
            template.format(exp=exp, member=member, dates=dates): 2
            for exp, member, dates in [
                ("historical", "r1i1p1f1", "185001-194912"),
                ("historical", "r1i1p1f1", "195001-201412"),
                ("ssp585", "r1i1p1f1", "201501-210012"),
                # This ensemble member has no data for the other experiment.
                ("historical", "r2i1p1f1", "185001-201412"),
            ]
        },
    )
    _use_globus(monkeypatch, client)
    facets = {
        "dataset": "CESM2",
        "exp": ["historical", "ssp585"],
        "short_name": "tas",
    }
    assert esgf.is_searchable("CMIP6", "timerange")
    assert esgf.facet_values("CMIP6", facets, "timerange") == [
        "*",
        "1850/2014",
        "1850/2100",
    ]
    (request,) = client.requests
    assert request["limit"] == 0
    assert request["facets"][0]["size"] == esgf._MAX_FILE_NAMES
    assert {
        "type": "match_any",
        "field_name": "type",
        "values": ["File"],
    } in request["filters"]


def test_globus_time_values_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _GlobusFiles({})
    _use_globus(monkeypatch, client)
    assert esgf.facet_values("CMIP6", {"exp": "ssp585"}, "timerange") == []
    assert len(client.requests) == 1


_DATASET_DATES = {
    "datetime_start": {"1850-01-16T12:00:00.000Z": 20},
    # Datasets have one of the fields of the last date.
    "datetime_end": {"2014-12-16T12:00:00.000Z": 8, "2014-11-06T00:00:00Z": 9},
    "datetime_stop": {"2014-12-16T12:00:00.000Z": 12},
}


@pytest.mark.parametrize(
    "names",
    [
        pytest.param(
            {
                "tas_A1_185001-201412.nc": 1,
                "tas_A1.SRESA1B_1.CCSM.atmm.2000-01_cat_2099-12.nc": 1,
            },
            id="cannot be read",
        ),
        pytest.param(
            {f"tas_{i}_185001-185012.nc": 1 for i in range(5000)},
            id="too many",
        ),
    ],
)
def test_globus_time_values_from_datasets(
    monkeypatch: pytest.MonkeyPatch,
    names: dict[str, int],
) -> None:
    _use_globus(monkeypatch, _GlobusFiles(names, _DATASET_DATES))
    facets = {"dataset": "CESM2", "exp": "historical"}
    assert esgf.facet_values("CMIP6", facets, "timerange") == [
        "*",
        "1850/2014",
    ]


def test_globus_time_values_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_globus(monkeypatch, _GlobusClient({}))
    assert esgf.facet_values("CMIP6", {"exp": "ssp585"}, "timerange") == []
    assert "Failed to get the dates from" in caplog.text


@pytest.mark.parametrize("project", ["CMIP7", "CORDEX-CMIP6"])
def test_globus_time_values_only_on_stac(
    monkeypatch: pytest.MonkeyPatch,
    project: str,
) -> None:
    client = _GlobusFiles({})
    _use_globus(monkeypatch, client)
    assert not esgf.is_searchable(project, "timerange")
    assert not client.requests
