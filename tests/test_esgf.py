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
    mapping = esgf._intake_esgf_mapping("CMIP5")
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


@pytest.mark.parametrize(
    "mapping",
    [esgf._intake_esgf_mapping("CMIP6"), esgf._solr_mapping("CMIP6")],
)
def test_version_mapping(mapping: esgf._Mapping | None) -> None:
    assert mapping is not None
    query = mapping.query({"version": "v20190429"}, "dataset")
    assert query == {"version": ["20190429"]}
    assert mapping.value("version", "20190429") == "v20190429"


def test_unknown_project_mapping() -> None:
    assert esgf._intake_esgf_mapping("OBS6") is None
    assert esgf._solr_mapping("OBS6") is None


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
