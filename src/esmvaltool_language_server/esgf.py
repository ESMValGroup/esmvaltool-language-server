"""Find the values of facets of the datasets that are available on ESGF.

The values are found by asking the ESGF indices that are enabled in the
`intake-esgf configuration <https://intake-esgf.readthedocs.io/en/latest/configure.html>`_
for the number of datasets with each value, which takes less than a second,
instead of searching for the datasets, which can take minutes.
The facet names and values in the recipe are translated to those on ESGF with
the configuration of the intake-esgf data source of ESMValCore for Globus and
STAC indices, and with the facet mapping of :mod:`esmvalcore.io.esgf` for
Solr indices. The version is not part of these mappings, ESMValCore adds the
prefix "v" to the version of a dataset on ESGF.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import esmvalcore.config
import globus_sdk
import intake_esgf
import requests
import yaml
from esmvalcore.config import CFG
from esmvalcore.io.esgf.facets import DATASET_MAP, FACETS
from intake_esgf.core.globus import GlobusESGFIndex
from intake_esgf.core.solr import SolrESGFIndex
from intake_esgf.core.stac import STACESGFIndex

from esmvaltool_language_server.cmor import is_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    Index = GlobusESGFIndex | SolrESGFIndex | STACESGFIndex
    Query = dict[str, list[str]]

logger = logging.getLogger(__name__)

_INTAKE_ESGF_DATA_SOURCE = "esmvalcore.io.intake_esgf.IntakeESGFDataSource"

# Stop waiting for an index after this many seconds.
TIMEOUT = 10

# The maximum number of values of a facet to get from an index.
_MAX_VALUES = 1000

# Errors when an index cannot be reached or gives an unexpected answer.
_ERRORS = (requests.RequestException, globus_sdk.GlobusError, LookupError)

# The prefix that ESMValCore adds to the version of a dataset on ESGF.
_VERSION_PREFIX = "v"


@dataclasses.dataclass(frozen=True)
class _Mapping:
    """Mapping between the facets in a recipe and those on ESGF."""

    facets: dict[str, str]
    """The ESGF name of each facet."""
    values: dict[str, dict[str, str]]
    """The ESGF value of each value of a facet, if it is different."""

    def query(self, facets: Mapping[object, object], facet: str) -> Query:
        """Translate the facets that select the values of a facet."""
        query = {}
        for ours, theirs in self.facets.items():
            values = facets.get(ours)
            values = values if isinstance(values, list) else [values]
            if ours != facet and values and all(map(is_name, values)):
                query[theirs] = [self._esgf_value(ours, v) for v in values]
        return query

    def _esgf_value(self, facet: str, value: str) -> str:
        """Translate a value in a recipe to the value on ESGF."""
        if facet == "version":
            return value.removeprefix(_VERSION_PREFIX)
        return self.values.get(facet, {}).get(value, value)

    def value(self, facet: str, value: str) -> str:
        """Translate a value on ESGF to the value in a recipe."""
        if facet == "version":
            return f"{_VERSION_PREFIX}{value}"
        for ours, theirs in self.values.get(facet, {}).items():
            if theirs == value:
                return ours
        return value


@functools.cache
def _intake_esgf_configuration() -> dict[str, Any]:
    path = (
        Path(esmvalcore.config.__file__).parent
        / "configurations"
        / "data-intake-esgf.yml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))["projects"]


def _intake_esgf_mapping(project: str) -> _Mapping | None:
    """Get the mapping of the configured or default intake-esgf data source."""
    for settings in (CFG["projects"], _intake_esgf_configuration()):
        for source in settings.get(project, {}).get("data", {}).values():
            if source.get("type") == _INTAKE_ESGF_DATA_SOURCE:
                return _Mapping(
                    {**source["facets"], "version": "version"},
                    source.get("values", {}),
                )
    return None


def _solr_mapping(project: str) -> _Mapping | None:
    if project not in FACETS:
        return None
    return _Mapping(
        {"project": "project", **FACETS[project], "version": "version"},
        {"dataset": DATASET_MAP.get(project, {})},
    )


def _mapping(index: Index, project: str) -> _Mapping | None:
    if isinstance(index, SolrESGFIndex):
        return _solr_mapping(project)
    return _intake_esgf_mapping(project)


@functools.cache
def _indices() -> list[Index]:
    """Get the indices that are enabled in the intake-esgf configuration."""
    indices: list[Index] = []
    for kind, cls in (
        ("globus_indices", GlobusESGFIndex),
        ("solr_indices", SolrESGFIndex),
        ("stac_indices", STACESGFIndex),
    ):
        indices.extend(
            cls(name)
            for name, enabled in intake_esgf.conf[kind].items()
            if enabled
        )
    return indices


def _globus_values(
    index: GlobusESGFIndex,
    query: Query,
    field: str,
) -> dict[str, int]:
    filters = [
        {"type": "match_any", "field_name": name, "values": values}
        for name, values in [
            *query.items(),
            ("type", ["Dataset"]),
            ("latest", [True]),
            ("retracted", [False]),
        ]
    ]
    facet = {
        "type": "terms",
        "name": field,
        "field_name": field,
        "size": _MAX_VALUES,
    }
    response = index.client.post_search(
        index.index_id,
        {"q": "*", "limit": 0, "filters": filters, "facets": [facet]},
    )
    return {
        bucket["value"]: bucket["count"]
        for bucket in response["facet_results"][0]["buckets"]
    }


def _solr_values(
    index: SolrESGFIndex,
    query: Query,
    field: str,
) -> dict[str, int]:
    params = [
        (name, value) for name, values in query.items() for value in values
    ]
    params.extend(
        [
            ("type", "Dataset"),
            ("latest", "true"),
            ("retracted", "false"),
            ("distrib", str(index.distrib).lower()),
            ("limit", "0"),
            ("facets", field),
            ("format", "application/solr+json"),
        ],
    )
    response = index.session.get(
        f"{index.url}/esg-search/search",
        params=params,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    counts = response.json()["facet_counts"]["facet_fields"][field]
    return dict(zip(counts[::2], counts[1::2], strict=True))


@dataclasses.dataclass(frozen=True)
class _Collection:
    """A STAC collection with the properties that can be searched."""

    url: str
    queryables: frozenset[str]
    aggregations: frozenset[str]


@functools.cache
def _stac_collection(url: str, project: str) -> _Collection | None:
    """Find the collection of a project in a STAC catalog."""
    session = intake_esgf.conf.get_cached_session()

    def get(path: str) -> dict[str, Any]:
        response = session.get(path, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    base = f"https://{url}/collections"
    for collection in get(base)["collections"]:
        if collection["id"].lower() == project.lower():
            path = f"{base}/{collection['id']}"
            try:
                aggregations = get(f"{path}/aggregations")["aggregations"]
            except requests.HTTPError as exc:
                logger.warning("Cannot count the values of facets: %s", exc)
                aggregations = []
            return _Collection(
                url=path,
                queryables=frozenset(get(f"{path}/queryables")["properties"]),
                aggregations=frozenset(a["name"] for a in aggregations),
            )
    return None


def _cql2_text(name: str, values: list[str]) -> str:
    quoted = ", ".join("'{}'".format(v.replace("'", "''")) for v in values)
    return f'"{name}" IN ({quoted})'


def _stac_values(
    index: STACESGFIndex,
    query: Query,
    field: str,
) -> dict[str, int]:
    project = query.get("project", [""])[0]
    collection = _stac_collection(index.url, project)
    if collection is None:
        return {}

    def name(field: str) -> str:
        # Properties that are specific to a project start with its name.
        prefixed = f"{project.lower()}:{field}"
        return prefixed if prefixed in collection.queryables else field

    aggregation = name(field).replace(":", "_") + "_frequency"
    if aggregation not in collection.aggregations:
        return {}
    conditions = [
        _cql2_text(name(key), values)
        for key, values in query.items()
        if key != "project" and name(key) in collection.queryables
    ]
    conditions.extend(
        f"{key}={value}"
        for key, value in (("latest", "true"), ("retracted", "false"))
        if key in collection.queryables
    )
    response = index.session.get(
        f"{collection.url}/aggregate",
        params={
            "aggregations": aggregation,
            "filter-lang": "cql2-text",
            "filter": " AND ".join(conditions),
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return {
        bucket["key"]: bucket["frequency"]
        for bucket in response.json()["aggregations"][0]["buckets"]
    }


def _index_values(
    index: Index,
    query: Query,
    field: str,
) -> dict[str, int]:
    """Get the number of datasets with each value of a field on an index."""
    try:
        if isinstance(index, GlobusESGFIndex):
            return _globus_values(index, query, field)
        if isinstance(index, SolrESGFIndex):
            return _solr_values(index, query, field)
        return _stac_values(index, query, field)
    except _ERRORS as exc:
        logger.warning(
            "Failed to get the values of %s from %s: %s", field, index, exc
        )
        return {}


def known_facets(project: str) -> set[str]:
    """Get the facets of a project that are known on the enabled indices.

    Parameters
    ----------
    project:
        The project.

    Returns
    -------
    :
        The facets.
    """
    return {
        facet
        for mapping in (_mapping(index, project) for index in _indices())
        if mapping is not None
        for facet in mapping.facets
    }


def is_searchable(project: str, facet: str) -> bool:
    """Check if the values of a facet of a project can be found on ESGF.

    Parameters
    ----------
    project:
        The project.
    facet:
        The facet.

    Returns
    -------
    :
        Whether the facet is known on one of the enabled indices.
    """
    return facet != "project" and facet in known_facets(project)


def facet_values(
    project: str,
    facets: Mapping[object, object],
    facet: str,
) -> list[str]:
    """Get the values of a facet of the datasets on ESGF that match facets.

    Facets with glob patterns are ignored.

    Parameters
    ----------
    project:
        The project of the datasets.
    facets:
        The facets of the datasets.
    facet:
        The facet to get the values of.

    Returns
    -------
    :
        The values, sorted. Indices that cannot be reached are skipped.
    """
    jobs = [
        (index, mapping)
        for index in _indices()
        if (mapping := _mapping(index, project)) is not None
        and facet in mapping.facets
    ]

    def search(job: tuple[Index, _Mapping]) -> set[str]:
        index, mapping = job
        query = mapping.query({**facets, "project": project}, facet)
        counts = _index_values(index, query, mapping.facets[facet])
        return {mapping.value(facet, value) for value in counts}

    with ThreadPoolExecutor(max_workers=max(len(jobs), 1)) as pool:
        return sorted(set().union(*pool.map(search, jobs)))
