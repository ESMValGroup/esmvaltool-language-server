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
The time range of the datasets is found once the experiment is known: from
the names of the files that match on Globus indices, by asking for the number
of files with each name, from the first and last date of the files of up to
100 matching datasets on STAC indices, and from the most common first and last
date of the matching datasets on Solr indices.
Globus indices also use these most common dates if they have too many files or
if the dates cannot be read from the names of the files, as in many CMIP3
files. The dates of a dataset can differ between the copies on different nodes
and some are wrong, so the names of the files are preferred.
Like ESMValCore, the dates of files are read from their names.
The time ranges of the experiments of a dataset are joined, but not across
years without data, because ESMValCore stops if there is no data for a year.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import re
import threading
from collections import Counter
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

# The number of requests that failed, by source of the data.
_FAILURES: Counter[str] = Counter()

# The prefix that ESMValCore adds to the version of a dataset on ESGF.
_VERSION_PREFIX = "v"

# The maximum number of file names that a Globus index returns.
_MAX_FILE_NAMES = 5000

# Globus indices limit the number of requests and let a request wait a few
# seconds if there are too many, so they are sent one by one.
_GLOBUS_LOCK = threading.Lock()

# The field of the first date of a dataset.
_START_DATE = "datetime_start"

# The fields of the last date of a dataset. Datasets on Globus indices have
# one of these.
_SOLR_END_DATES = ("datetime_stop",)
_GLOBUS_END_DATES = ("datetime_end", "datetime_stop")

# The first and last date at the end of the name of a file on ESGF, e.g.
# tas_Amon_CESM2_historical_r1i1p1f1_gn_185001-201412.nc, with the suffix of
# climatologies. Files without a time have no dates.
_FILE_DATES = re.compile(r"_(\d{4,14})-(\d{4,14})(?:-clim)?\.nc$")

# The facet of the experiment. The time range of the datasets on ESGF is only
# found if the experiment is known.
_EXPERIMENT = "exp"

# Projects that are only published on the STAC indices.
_ONLY_ON_STAC = frozenset({"CMIP7", "CORDEX-CMIP6"})


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


def record_failure(source: object) -> None:
    """Remember that a search of a source of data failed.

    Parameters
    ----------
    source:
        The source of the data.
    """
    _FAILURES[str(source)] += 1


def failures() -> int:
    """Get the number of failed searches of sources of data since the start.

    A search that overlaps with a failed search cannot tell whether it was
    affected itself, so comparing this number can only tell that a search may
    have missed values.

    Returns
    -------
    :
        The number of failed searches.
    """
    return _FAILURES.total()


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
    if project not in FACETS:
        return None
    return _Mapping(
        {"project": "project", **FACETS[project], "version": "version"},
        {"dataset": DATASET_MAP.get(project, {})},
    )


def _mapping(index: Index, project: str) -> _Mapping | None:
    if project in _ONLY_ON_STAC and not isinstance(index, STACESGFIndex):
        return None
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
    for index in indices:
        if isinstance(index, GlobusESGFIndex):
            index.client.transport.http_timeout = TIMEOUT
    return indices


def _globus_values(
    index: GlobusESGFIndex,
    query: Query,
    field: str,
    kind: str = "Dataset",
    size: int = _MAX_VALUES,
) -> dict[str, int]:
    filters = [
        {"type": "match_any", "field_name": name, "values": values}
        for name, values in [
            *query.items(),
            ("type", [kind]),
            ("latest", [True]),
            ("retracted", [False]),
        ]
    ]
    facet = {
        "type": "terms",
        "name": field,
        "field_name": field,
        "size": size,
    }
    with _GLOBUS_LOCK:
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
    aggregations: frozenset[str] | None
    """The aggregations, None if the index cannot list them."""


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
            # Some indices fail to list the aggregations that they do support.
            try:
                aggregations = frozenset(
                    a["name"]
                    for a in get(f"{path}/aggregations")["aggregations"]
                )
            except requests.HTTPError as exc:
                logger.warning(
                    "Cannot count the values of facets for certain, "
                    "trying anyway: %s",
                    exc,
                )
                aggregations = None
            return _Collection(
                url=path,
                queryables=frozenset(get(f"{path}/queryables")["properties"]),
                aggregations=aggregations,
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


def _stac_selection(
    collection: _Collection,
    project: str,
    query: Query,
) -> tuple[list[tuple[str, list[str]]], list[tuple[str, bool]]]:
    """Get the values of the properties that can be searched on an index."""
    selection = [
        (name, values)
        for key, values in query.items()
        if key != "project"
        and (name := _stac_name(collection, project, key))
        in collection.queryables
    ]
    flags = [
        (name, value)
        for name, value in (("latest", True), ("retracted", False))
        if name in collection.queryables
    ]
    return selection, flags


def _stac_filter(
    collection: _Collection,
    project: str,
    query: Query,
) -> dict[str, str]:
    """Get the parameters that select the latest datasets matching a query."""
    selection, flags = _stac_selection(collection, project, query)
    conditions = [_cql2_text(name, values) for name, values in selection]
    conditions.extend(f"{name}={str(value).lower()}" for name, value in flags)
    return {"filter-lang": "cql2-text", "filter": " AND ".join(conditions)}


def _stac_json_filter(
    collection: _Collection,
    project: str,
    query: Query,
) -> dict[str, Any]:
    """Get the JSON version of the parameters of :func:`_stac_filter`."""
    selection, flags = _stac_selection(collection, project, query)
    conditions: list[dict[str, Any]] = [
        {"op": "in", "args": [{"property": name}, values]}
        for name, values in selection
    ]
    conditions.extend(
        {"op": "=", "args": [{"property": name}, value]}
        for name, value in flags
    )
    if not conditions:
        return {}
    return {
        "filter-lang": "cql2-json",
        "filter": conditions[0]
        if len(conditions) == 1
        else {"op": "and", "args": conditions},
    }


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
    if (
        collection.aggregations is not None
        and aggregation not in collection.aggregations
    ):
        return {}
    # Some indices ignore the filter of this request if it is sent with GET.
    response = index.session.post(
        f"{collection.url}/aggregate",
        json={
            "aggregations": [aggregation],
            # Some indices only return the 10 most common values by default.
            "size": _MAX_VALUES,
            **_stac_json_filter(collection, project, query),
        },
        timeout=TIMEOUT,
    )
    # An index that cannot list its aggregations may not have this one.
    if (
        collection.aggregations is None
        and requests.codes.bad_request
        <= response.status_code
        < requests.codes.server_error
    ):
        return {}
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
        record_failure(index)
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
        the time range and an index that knows the project is enabled.
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


def _time_indices(project: str) -> list[tuple[Index, _Mapping]]:
    """Get the enabled indices that know the dates of datasets of a project."""
    return [
        (index, mapping)
        for index in _indices()
        if (mapping := _mapping(index, project)) is not None
    ]


def _most_common(
    index: SolrESGFIndex | GlobusESGFIndex,
    query: Query,
    fields: Iterable[str],
) -> str | None:
    """Get the most common value of some fields of datasets on an index."""
    counts: Counter[str] = Counter()
    for field in fields:
        counts.update(_index_values(index, query, field))
    return max(counts, key=counts.__getitem__, default=None)


def _dataset_ranges(
    index: SolrESGFIndex | GlobusESGFIndex,
    query: Query,
    experiment: str,
) -> list[tuple[str, str]]:
    """Get the most common first and last date of datasets on an index."""
    end_dates = (
        _GLOBUS_END_DATES
        if isinstance(index, GlobusESGFIndex)
        else _SOLR_END_DATES
    )

    def most_common(query: Query) -> tuple[str, str] | None:
        start = _most_common(index, query, [_START_DATE])
        end = _most_common(index, query, end_dates)
        if start is None or end is None:
            return None
        return timerange.from_iso(start, end)

    queries = [
        {**query, experiment: [exp]} for exp in query.get(experiment, [])
    ] or [query]
    return timerange.join(
        r for q in queries if (r := most_common(q)) is not None
    )


def _globus_ranges(
    index: GlobusESGFIndex,
    query: Query,
    experiment: str,
) -> list[tuple[str, str]]:
    """Get the time ranges without missing years of datasets on Globus.

    The files of the experiments of a dataset are joined. The names of the
    files are found without searching for the datasets and their files first,
    which is much slower for models with many ensemble members.
    """
    names = _globus_values(
        index,
        query,
        "title",
        kind="File",
        size=_MAX_FILE_NAMES,
    )
    matches = [m for name in names if (m := _FILE_DATES.search(name))]
    if len(names) >= _MAX_FILE_NAMES or len(matches) < len(names):
        return _dataset_ranges(index, query, experiment)
    differ = set(query.get(experiment, []))
    datasets: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for match in matches:
        parts = match.string[: match.start()].split("_")
        key = tuple(part for part in parts if part not in differ)
        datasets.setdefault(key, []).append((match[1], match[2]))
    return [r for found in datasets.values() for r in timerange.join(found)]


def _dataset_key(item: dict[str, Any], names: Iterable[str]) -> str:
    """Get the ID of a dataset without the parts that differ by experiment.

    These are the experiment, the activity, and the version at the end.
    """
    parts = str(item.get("id", "")).split(".")[:-1]
    differ = set()
    for name in names:
        values = item.get("properties", {}).get(name, [])
        differ.update(values if isinstance(values, list) else [values])
    return ".".join(p for p in parts if p not in differ)


def _file_ranges(item: dict[str, Any]) -> set[tuple[str, str]]:
    """Get the first and last date of the files of a dataset on STAC."""
    return {
        (match[1], match[2])
        for asset in item.get("assets", {}).values()
        if (match := _FILE_DATES.search(str(asset.get("href", ""))))
    }


def _stac_ranges(
    index: STACESGFIndex,
    query: Query,
    experiment: str,
) -> list[tuple[str, str]]:
    """Get the time ranges without missing years of datasets on STAC.

    The files of the experiments of a dataset are joined.
    """
    project = query.get("project", [""])[0]
    collection = _stac_collection(index.url, project)
    if collection is None:
        return []
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
                ["id", "assets", *(f"properties.{n}" for n in differ)],
            ),
            **_stac_filter(collection, project, query),
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    datasets: dict[str, list[tuple[str, str]]] = {}
    for item in response.json()["features"]:
        key = _dataset_key(item, differ)
        datasets.setdefault(key, []).extend(_file_ranges(item))
    return [r for found in datasets.values() for r in timerange.join(found)]


def _index_ranges(
    index: Index,
    query: Query,
    experiment: str,
) -> list[tuple[str, str]]:
    """Get the first and last date of datasets on an index.

    The time ranges of the experiments of a dataset are joined, like
    ESMValCore joins the data of a list of experiments.
    """
    try:
        if isinstance(index, GlobusESGFIndex):
            return _globus_ranges(index, query, experiment)
        if isinstance(index, SolrESGFIndex):
            return _dataset_ranges(index, query, experiment)
        return _stac_ranges(index, query, experiment)
    except _ERRORS as exc:
        logger.warning("Failed to get the dates from %s: %s", index, exc)
        record_failure(index)
        return []


def _time_values(
    project: str,
    facets: Mapping[object, object],
    facet: str,
) -> list[str]:
    """Get the values of a time facet from the dates of datasets."""
    jobs = _time_indices(project)
    if not jobs:
        return []

    def search(job: tuple[Index, _Mapping]) -> list[tuple[str, str]]:
        index, mapping = job
        query = mapping.query({**facets, "project": project}, facet)
        # Datasets of different experiments cover different time ranges and
        # there may be too many of them to get all of their dates.
        experiment = mapping.facets.get(_EXPERIMENT, "")
        if experiment and experiment not in query:
            return []
        return _index_ranges(index, query, experiment)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        ranges = [r for found in pool.map(search, jobs) for r in found]
    return timerange.suggestions(facet, ranges)
