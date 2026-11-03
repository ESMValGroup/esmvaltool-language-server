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
The time range of the datasets is found from the most common first and last
date of the matching datasets on Solr indices, and from the first and last
date of up to 100 matching datasets on STAC indices, once the experiment
is known.
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

from esmvaltool_language_server import timerange
from esmvaltool_language_server.cmor import is_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    Index = GlobusESGFIndex | SolrESGFIndex | STACESGFIndex
    Query = dict[str, list[str]]

logger = logging.getLogger(__name__)

_INTAKE_ESGF_DATA_SOURCE = "esmvalcore.io.intake_esgf.IntakeESGFDataSource"

# Stop waiting for an index after this many seconds.
TIMEOUT = 10

# The maximum number of values of a facet to get from an index.
_MAX_VALUES = 1000

# The maximum number of datasets to get the dates of from a STAC index. Some
# indices send the complete datasets, which are about 100 kB each.
_MAX_DATASETS = 100

# Errors when an index cannot be reached or gives an unexpected answer.
_ERRORS = (requests.RequestException, globus_sdk.GlobusError, LookupError)

# The prefix that ESMValCore adds to the version of a dataset on ESGF.
_VERSION_PREFIX = "v"

# The fields of the first and last date of a dataset on Solr indices.
_SOLR_DATES = ("datetime_start", "datetime_stop")

# The facet of the experiment. The time range of the datasets on ESGF is only
# found if the experiment is known.
_EXPERIMENT = "exp"

# Projects that are not published on the Solr indices.
_NOT_ON_SOLR = frozenset({"CMIP7", "CORDEX-CMIP6"})


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


def intake_esgf_mapping(project: str) -> _Mapping | None:
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
    if project not in FACETS or project in _NOT_ON_SOLR:
        return None
    return _Mapping(
        {"project": "project", **FACETS[project], "version": "version"},
        {"dataset": DATASET_MAP.get(project, {})},
    )


def _mapping(index: Index, project: str) -> _Mapping | None:
    if isinstance(index, SolrESGFIndex):
        return _solr_mapping(project)
    return intake_esgf_mapping(project)


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


def _stac_name(collection: _Collection, project: str, field: str) -> str:
    """Get the name of a property in a STAC collection."""
    # Properties that are specific to a project start with its name.
    prefixed = f"{project.lower()}:{field}"
    return prefixed if prefixed in collection.queryables else field


def _stac_filter(
    collection: _Collection,
    project: str,
    query: Query,
) -> dict[str, str]:
    """Get the parameters that select the latest datasets matching a query."""
    conditions = [
        _cql2_text(name, values)
        for key, values in query.items()
        if key != "project"
        and (name := _stac_name(collection, project, key))
        in collection.queryables
    ]
    conditions.extend(
        f"{key}={value}"
        for key, value in (("latest", "true"), ("retracted", "false"))
        if key in collection.queryables
    )
    return {"filter-lang": "cql2-text", "filter": " AND ".join(conditions)}


def _stac_values(
    index: STACESGFIndex,
    query: Query,
    field: str,
) -> dict[str, int]:
    project = query.get("project", [""])[0]
    collection = _stac_collection(index.url, project)
    if collection is None:
        return {}
    name = _stac_name(collection, project, field)
    aggregation = name.replace(":", "_") + "_frequency"
    if aggregation not in collection.aggregations:
        return {}
    response = index.session.get(
        f"{collection.url}/aggregate",
        params={
            "aggregations": aggregation,
            **_stac_filter(collection, project, query),
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
        Whether the facet is known on one of the enabled indices, or selects
        the time range and a Solr or STAC index that knows the project is
        enabled.
    """
    if facet in timerange.FACETS:
        return bool(_time_indices(project))
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
    if facet in timerange.FACETS:
        return _time_values(project, facets, facet)
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


def _time_indices(project: str) -> list[SolrESGFIndex | STACESGFIndex]:
    """Get the enabled indices that know the dates of datasets of a project."""
    return [
        index
        for index in _indices()
        if isinstance(index, SolrESGFIndex | STACESGFIndex)
        and _mapping(index, project) is not None
    ]


def _join(ranges: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Join the time ranges of the experiments of a dataset.

    Like the check of the data availability of ESMValCore, years without
    data are gaps, so time ranges are only joined if there is no year
    between them, and a time range is suggested for each part.
    """
    joined: list[tuple[str, str]] = []
    for start, end in sorted(ranges):
        if joined and int(start[:4]) <= int(joined[-1][1][:4]) + 1:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


def _solr_ranges(
    index: SolrESGFIndex,
    query: Query,
    experiment: str,
) -> list[tuple[str, str]]:
    """Get the most common first and last date of datasets on Solr."""

    def most_common(query: Query) -> tuple[str, str] | None:
        start, end = (
            max(counts, key=counts.__getitem__, default=None)
            for counts in (_index_values(index, query, f) for f in _SOLR_DATES)
        )
        if start is None or end is None:
            return None
        # The frequency is not known, so the dates are given in years.
        return timerange.from_iso(start, end, "yr")

    queries = [
        {**query, experiment: [exp]} for exp in query.get(experiment, [])
    ] or [query]
    return _join(r for q in queries if (r := most_common(q)) is not None)


def _dataset_key(item: dict[str, Any], names: Iterable[str]) -> str:
    """Get the ID of a dataset without the parts that differ by experiment.

    These are the experiment, the activity, and the version at the end.
    """
    parts = str(item.get("id", "")).split(".")[:-1]
    differ = set()
    for name in names:
        values = item["properties"].get(name, [])
        differ.update(values if isinstance(values, list) else [values])
    return ".".join(p for p in parts if p not in differ)


def _stac_ranges(
    index: STACESGFIndex,
    query: Query,
    experiment: str,
) -> list[tuple[str, str]]:
    """Get the first and last date of the datasets on STAC.

    The time ranges of the experiments of a dataset are joined.
    """
    project = query.get("project", [""])[0]
    collection = _stac_collection(index.url, project)
    if collection is None:
        return []
    fields = ("start_datetime", "end_datetime", "frequency")
    names = [_stac_name(collection, project, f) for f in fields]
    differ = [
        _stac_name(collection, project, f) for f in (experiment, "activity_id")
    ]
    # Some indices ignore the filter of the items of a collection, but not
    # that of a search.
    response = index.session.get(
        f"https://{index.url}/search",
        params={
            "collections": collection.url.rsplit("/", 1)[-1],
            "limit": _MAX_DATASETS,
            "fields": ",".join(
                ["id", *(f"properties.{n}" for n in [*names, *differ])],
            ),
            **_stac_filter(collection, project, query),
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    datasets: dict[str, list[tuple[str, str]]] = {}
    for item in response.json()["features"]:
        start, end, frequency = (item["properties"].get(n) for n in names)
        found = (
            timerange.from_iso(start, end, frequency)
            if start and end
            else None
        )
        if found is not None:
            datasets.setdefault(_dataset_key(item, differ), []).append(found)
    return [r for found in datasets.values() for r in _join(found)]


def _index_ranges(
    index: SolrESGFIndex | STACESGFIndex,
    query: Query,
    experiment: str,
) -> list[tuple[str, str]]:
    """Get the first and last date of datasets on an index.

    The time ranges of the experiments of a dataset are joined, like
    ESMValCore joins the data of a list of experiments.
    """
    try:
        if isinstance(index, SolrESGFIndex):
            return _solr_ranges(index, query, experiment)
        return _stac_ranges(index, query, experiment)
    except _ERRORS as exc:
        logger.warning("Failed to get the dates from %s: %s", index, exc)
        return []


def _time_values(
    project: str,
    facets: Mapping[object, object],
    facet: str,
) -> list[str]:
    """Get the values of a time facet from the dates of datasets."""
    indices = _time_indices(project)
    if not indices:
        return []

    def search(index: SolrESGFIndex | STACESGFIndex) -> list[tuple[str, str]]:
        mapping = _mapping(index, project)
        if mapping is None:
            return []
        query = mapping.query({**facets, "project": project}, facet)
        # Datasets of different experiments cover different time ranges and
        # there may be too many of them to get all of their dates.
        experiment = mapping.facets.get(_EXPERIMENT, "")
        if experiment and experiment not in query:
            return []
        return _index_ranges(index, query, experiment)

    with ThreadPoolExecutor(max_workers=len(indices)) as pool:
        ranges = [r for found in pool.map(search, indices) for r in found]
    return timerange.suggestions(facet, ranges)
