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
To replace glob patterns by datasets, :func:`datasets` asks each index for
all the matching datasets at once, with their dates, refer to
:func:`_with_data`.
"""

from __future__ import annotations

import dataclasses
import functools
import itertools
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
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
    from collections.abc import Callable, Iterable, Mapping

    Index = GlobusESGFIndex | SolrESGFIndex | STACESGFIndex
    Query = dict[str, list[str]]

logger = logging.getLogger(__name__)

_INTAKE_ESGF_DATA_SOURCE = "esmvalcore.io.intake_esgf.IntakeESGFDataSource"

# Stop waiting for an index after this many seconds.
TIMEOUT = 10

# Do not ask an index again for this many seconds if it cannot be reached.
RETRY_AFTER = 60

# The maximum number of values of a facet to get from an index.
_MAX_VALUES = 1000

# The maximum number of datasets to get the dates of from a STAC index. Some
# indices send the complete datasets, which are about 100 kB each.
_MAX_DATASETS = 100

# Errors when an index cannot be reached or gives an unexpected answer.
_ERRORS = (requests.RequestException, globus_sdk.GlobusError, LookupError)

# Errors when an index cannot be reached.
_UNREACHABLE = (
    requests.ConnectionError,
    requests.Timeout,
    globus_sdk.NetworkError,
)

# When to ask an index again that could not be reached, by index.
_RETRY_AT: dict[Index, float] = {}

# The problems of the requests that failed, with the number of requests that
# had failed when each problem was last seen.
_FAILURES: dict[str, int] = {}
_FAILURES_LOCK = threading.Lock()

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


def record_failure(source: object, reason: str) -> None:
    """Remember that a search of a source of data failed.

    Parameters
    ----------
    source:
        The source of the data.
    reason:
        Why the search failed, for the user, e.g. ``could not be reached``.
    """
    with _FAILURES_LOCK:
        _FAILURES[f"{source} {reason}"] = _failed() + 1


def _failed() -> int:
    return max(_FAILURES.values(), default=0)


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
    with _FAILURES_LOCK:
        return _failed()


def failures_since(count: int) -> list[str]:
    """Describe the failed searches since a number of failed searches.

    Parameters
    ----------
    count:
        The number of failed searches, from :func:`failures`.

    Returns
    -------
    :
        The problems, e.g. ``SolrESGFIndex('esgf-node.ornl.gov') could not
        be reached``, sorted.
    """
    with _FAILURES_LOCK:
        return sorted(p for p, last in _FAILURES.items() if last > count)


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


def _session() -> requests.Session:
    """Create a session to send requests to an index.

    The sessions of intake-esgf share a cache in an SQLite database that
    does not support sending requests from several threads at once, so a
    session without a cache is used. The values are remembered by
    :class:`esmvaltool_language_server.search.FacetSearch` instead.
    """
    return requests.Session()


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
        index.session = _session()
        if isinstance(index, GlobusESGFIndex):
            index.client.transport.http_timeout = TIMEOUT
    return indices


def _globus_filters(query: Query, kind: str) -> list[dict[str, Any]]:
    """Select the latest datasets or files on Globus that match a query."""
    return [
        {"type": "match_any", "field_name": name, "values": values}
        for name, values in [
            *query.items(),
            ("type", [kind]),
            ("latest", [True]),
            ("retracted", [False]),
        ]
    ]


def _globus_values(
    index: GlobusESGFIndex,
    query: Query,
    field: str,
    kind: str = "Dataset",
    size: int = _MAX_VALUES,
) -> dict[str, int]:
    filters = _globus_filters(query, kind)
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
    counts: dict[str, bool] = dataclasses.field(
        default_factory=dict,
        compare=False,
        hash=False,
    )
    """Whether the index has counted the values of each aggregation.

    Indices return no values for properties that they cannot count.
    """


@functools.cache
def _stac_collection(url: str, project: str) -> _Collection | None:
    """Find the collection of a project in a STAC catalog."""
    session = _session()

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
    # The names of aggregations have no colons or hyphens, e.g.
    # cordex_cmip6_source_id_frequency.
    aggregation = re.sub(r"[:-]", "_", name) + "_frequency"
    if (
        collection.aggregations is not None
        and aggregation not in collection.aggregations
    ):
        return {}
    if collection.counts.get(aggregation) is not False:
        counts = _stac_counts(index, collection, project, query, aggregation)
        if counts is None:
            return {}
        if counts:
            collection.counts[aggregation] = True
        if counts or collection.counts.get(aggregation):
            return counts
    # Some indices cannot aggregate some properties, e.g. the version.
    values = _stac_item_values(index, collection, project, query, name)
    if values:
        collection.counts[aggregation] = False
    return values


def _stac_counts(
    index: STACESGFIndex,
    collection: _Collection,
    project: str,
    query: Query,
    aggregation: str,
) -> dict[str, int] | None:
    """Count the values of a property, None if the index cannot."""
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
        return None
    response.raise_for_status()
    return {
        bucket["key"]: bucket["frequency"]
        for bucket in response.json()["aggregations"][0]["buckets"]
    }


def _stac_item_values(
    index: STACESGFIndex,
    collection: _Collection,
    project: str,
    query: Query,
    name: str,
) -> dict[str, int]:
    """Count the values of a property in the first datasets on STAC.

    If there are more datasets, values may be missing, which is recorded as
    a failure.
    """
    response = index.session.get(
        f"https://{index.url}/search",
        params={
            "collections": collection.url.rsplit("/", 1)[-1],
            "limit": _MAX_DATASETS,
            "fields": f"id,properties.{name}",
            **_stac_filter(collection, project, query),
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    items = response.json()["features"]
    if len(items) >= _MAX_DATASETS:
        logger.warning(
            "Only read the %s of the first %s datasets on %s",
            name,
            len(items),
            index,
        )
        record_failure(
            index,
            f"found more datasets than the {len(items)} that were read",
        )
    values = (item.get("properties", {}).get(name) for item in items)
    return Counter(str(v) for v in values if v is not None)


def _ask[T](index: Index, what: str, ask: Callable[[], T], default: T) -> T:
    """Ask an index, unless it could not be reached recently.

    The default is returned if the index cannot be asked or fails to answer.
    """
    if time.monotonic() < _RETRY_AT.get(index, 0):
        record_failure(index, "could not be reached")
        return default
    try:
        return ask()
    except _UNREACHABLE as exc:
        _RETRY_AT[index] = time.monotonic() + RETRY_AFTER
        logger.warning(
            "Not asking %s for %s seconds: %s", index, RETRY_AFTER, exc
        )
        record_failure(index, "could not be reached")
    except _ERRORS as exc:
        logger.warning("Failed to get %s from %s: %s", what, index, exc)
        record_failure(index, f"failed to give {what}: {exc}")
    return default


def _index_values(
    index: Index,
    query: Query,
    field: str,
) -> dict[str, int]:
    """Get the number of datasets with each value of a field on an index."""

    def ask() -> dict[str, int]:
        if isinstance(index, GlobusESGFIndex):
            return _globus_values(index, query, field)
        if isinstance(index, SolrESGFIndex):
            return _solr_values(index, query, field)
        return _stac_values(index, query, field)

    return _ask(index, f"the values of {field}", ask, {})


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

    def ask() -> list[tuple[str, str]]:
        if isinstance(index, GlobusESGFIndex):
            return _globus_ranges(index, query, experiment)
        if isinstance(index, SolrESGFIndex):
            return _dataset_ranges(index, query, experiment)
        return _stac_ranges(index, query, experiment)

    return _ask(index, "the dates", ask, [])


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


# The maximum number of datasets to get from an index in one search.
_MAX_RECORDS = 10_000

# The number of datasets or files to get from an index in one request.
_PAGE_SIZE = 1000

# The number of datasets to get the files of in one request to a Solr index,
# which takes them in the URL.
_SOLR_DATASETS_PER_REQUEST = 50

# The number of datasets to get the files of in one request to Globus.
_GLOBUS_DATASETS_PER_REQUEST = 200

# Stop waiting for an index to send a page of datasets after this many
# seconds.
_RECORDS_TIMEOUT = 30

# The dates of files without dates, which ESMValCore uses for any years.
_ANY_DATES = ("0000", "9999")

# The datasets on an index that searches for different time ranges share, by
# index and query, with the time they were asked for.
_COPIES: dict[
    tuple[object, str],
    tuple[float, Future[list[_Copy] | None]],
] = {}
_COPIES_LOCK = threading.Lock()

# Ask an index for the datasets again after this many seconds.
_COPIES_AGE = 10 * 60


@dataclasses.dataclass(frozen=True)
class _Copy:
    """A copy of a dataset on an index."""

    index: Index
    id: str
    """The ID of the dataset on the index, to find its files."""
    values: dict[str, tuple[str, ...]]
    """The values of the facets in the recipe."""
    dates: tuple[tuple[str, str], ...]
    """The first and last date of the files, or of the dataset."""
    from_files: bool
    """Whether the dates are those of the files."""


def datasets(
    project: str,
    facets: Mapping[object, object],
    fields: tuple[str, ...],
) -> list[tuple[str, ...]]:
    """Get the values of facets of the datasets on ESGF that match facets.

    Each index is asked for all the matching datasets at once.
    A dataset is the same on all indices and for all experiments if it has
    the same values of the fields.
    For a list of experiments, a dataset needs data for each experiment.
    With a ``timerange`` of years, it needs data for each year, refer to
    :func:`_with_data`.
    Facets with glob patterns are ignored.

    Parameters
    ----------
    project:
        The project of the datasets.
    facets:
        The facets of the datasets.
    fields:
        The facets to get the values of.

    Returns
    -------
    :
        The values of the fields of each dataset, sorted. Indices that cannot
        be reached are skipped.
    """
    jobs = [
        (index, mapping)
        for index in _indices()
        if (mapping := _mapping(index, project)) is not None
        and set(fields) <= set(mapping.facets)
    ]

    def search(job: tuple[Index, _Mapping]) -> list[_Copy]:
        index, mapping = job
        query = mapping.query({**facets, "project": project}, "")
        return _shared_copies(index, mapping, query)

    with ThreadPoolExecutor(max_workers=max(len(jobs), 1)) as pool:
        copies = [c for found in pool.map(search, jobs) for c in found]
    found = _join(copies, facets, fields)
    years = timerange.selected_years(facets)
    if years is not None:
        found = _with_data(found, years)
    return sorted(found)


def _shared_copies(
    index: Index,
    mapping: _Mapping,
    query: Query,
) -> list[_Copy]:
    """Get the datasets on an index that match a query.

    Searches for different time ranges ask for the same datasets, so the
    requests that run at the same time or recently are shared. Datasets that
    could not be found are asked for again.
    """
    key = (index, repr(sorted(query.items())))
    now = time.monotonic()
    with _COPIES_LOCK:
        for old in [k for k, v in _COPIES.items() if v[0] < now - _COPIES_AGE]:
            del _COPIES[old]
        owner = key not in _COPIES
        if owner:
            _COPIES[key] = (now, Future())
        future = _COPIES[key][1]
    if owner:
        try:
            copies = _ask(
                index,
                "the datasets",
                lambda: _index_copies(index, mapping, query),
                None,
            )
        except BaseException as exc:
            with _COPIES_LOCK:
                del _COPIES[key]
            future.set_exception(exc)
            raise
        if copies is None:
            with _COPIES_LOCK:
                del _COPIES[key]
        future.set_result(copies)
    copies = future.result()
    if copies is None:
        if not owner:
            record_failure(index, "failed to give the datasets")
        return []
    return copies


def _join(
    copies: Iterable[_Copy],
    facets: Mapping[object, object],
    fields: tuple[str, ...],
) -> dict[tuple[str, ...], list[_Copy]]:
    """Join the copies and experiments of the datasets with data.

    Only the datasets with data for each experiment of a list are kept.
    """
    experiments = facets.get(_EXPERIMENT)
    needed = (
        set(map(str, experiments)) if isinstance(experiments, list) else set()
    )
    found: dict[tuple[str, ...], dict[str, list[_Copy]]] = {}
    for copy in copies:
        for row in itertools.product(
            *(copy.values.get(f, ()) for f in fields)
        ):
            for experiment in copy.values.get(_EXPERIMENT) or ("",):
                found.setdefault(row, {}).setdefault(experiment, []).append(
                    copy,
                )
    return {
        row: [c for copies in by_experiment.values() for c in copies]
        for row, by_experiment in found.items()
        if needed <= set(by_experiment)
    }


def _with_data(
    found: dict[tuple[str, ...], list[_Copy]],
    years: tuple[int, int],
) -> dict[tuple[str, ...], list[_Copy]]:
    """Get the datasets with data for each year.

    The dates of the copies of a dataset, and of its experiments, are joined.
    A dataset has data if its dates cover the years. The dates of some
    datasets on Solr and Globus indices are wrong, e.g. of some models with a
    360-day calendar, and Solr indices may only have some of the files, so
    the names of the files of the other datasets are asked for. A dataset is
    left out if these show that it has no data for some of the years.
    """
    unsure = {
        row: copies
        for row, copies in found.items()
        if not timerange.covers(years, (d for c in copies for d in c.dates))
    }
    files = _files(
        c for copies in unsure.values() for c in copies if not c.from_files
    )
    return {
        row: copies
        for row, copies in found.items()
        if row not in unsure or _files_cover(copies, files, years)
    }


def _files_cover(
    copies: Iterable[_Copy],
    files: Mapping[tuple[Index, str], list[tuple[str, str]]],
    years: tuple[int, int],
) -> bool:
    """Check if the files of the copies of a dataset cover years.

    True if the files of a copy are not known.
    """
    dates: list[tuple[str, str]] = []
    for copy in copies:
        found = (
            list(copy.dates)
            if copy.from_files
            else files.get((copy.index, copy.id))
        )
        if found is None:
            return True
        dates.extend(found)
    return timerange.covers(years, dates)


def _as_list(value: object) -> list[object]:
    """Get the values of a field that may have one or more values."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _index_copies(
    index: Index,
    mapping: _Mapping,
    query: Query,
) -> list[_Copy]:
    """Get the datasets on an index that match a query."""
    if isinstance(index, STACESGFIndex):
        return _stac_copies(index, mapping, query)
    records = (
        _globus_records(index, _globus_filters(query, "Dataset"))
        if isinstance(index, GlobusESGFIndex)
        else _solr_records(index, [*_solr_params(query), ("type", "Dataset")])
    )
    return [_record_copy(index, mapping, record) for record in records]


def _record_copy(
    index: Index,
    mapping: _Mapping,
    record: Mapping[str, Any],
) -> _Copy:
    """Get a copy of a dataset from its record on Solr or Globus."""
    start = record.get(_START_DATE)
    end = next(
        (record[f] for f in _GLOBUS_END_DATES if record.get(f)),
        None,
    )
    years = start and end and timerange.from_iso(str(start), str(end))
    return _Copy(
        index=index,
        id=str(record.get("id", "")),
        values=_values(mapping, record.get),
        # Datasets without dates, e.g. of time-independent variables.
        dates=(years,) if years else (_ANY_DATES,),
        from_files=False,
    )


def _values(
    mapping: _Mapping,
    get: Callable[[str], object],
) -> dict[str, tuple[str, ...]]:
    """Get the values of the facets in a recipe from the fields on ESGF."""
    return {
        facet: tuple(
            mapping.value(facet, str(v)) for v in _as_list(get(field))
        )
        for facet, field in mapping.facets.items()
    }


def _pages(
    index: Index,
    what: str,
    fetch: Callable[[int], tuple[list[dict[str, Any]], int]],
) -> list[dict[str, Any]]:
    """Get the records that match a search, one page at a time.

    If there are too many, the first ones are returned, which is recorded as
    a failure.
    """
    records: list[dict[str, Any]] = []
    while True:
        page, total = fetch(len(records))
        records.extend(page)
        if not page or len(records) >= min(total, _MAX_RECORDS):
            break
    if total > len(records):
        logger.warning(
            "Only read the first %s of %s %s on %s",
            len(records),
            total,
            what,
            index,
        )
        record_failure(
            index,
            f"found more {what} than the {len(records)} that were read",
        )
    return records


def _globus_records(
    index: GlobusESGFIndex,
    filters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Get the records on Globus that match filters."""

    def fetch(offset: int) -> tuple[list[dict[str, Any]], int]:
        with _GLOBUS_LOCK:
            response = index.client.post_search(
                index.index_id,
                {
                    "q": "*",
                    "filters": filters,
                    "limit": _PAGE_SIZE,
                    "offset": offset,
                },
            )
        return [
            r["entries"][0]["content"] for r in response["gmeta"]
        ], response["total"]

    return _pages(index, "records", fetch)


def _solr_params(query: Query) -> list[tuple[str, str]]:
    """Get the parameters of a request to Solr for the latest records."""
    return [
        *((name, value) for name, values in query.items() for value in values),
        ("latest", "true"),
        ("retracted", "false"),
        ("format", "application/solr+json"),
    ]


def _solr_records(
    index: SolrESGFIndex,
    params: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Get the records on Solr that match the parameters of a request."""

    def fetch(offset: int) -> tuple[list[dict[str, Any]], int]:
        response = index.session.get(
            f"{index.url}/esg-search/search",
            params=[
                *params,
                ("distrib", str(index.distrib).lower()),
                ("limit", str(_PAGE_SIZE)),
                ("offset", str(offset)),
            ],
            timeout=_RECORDS_TIMEOUT,
        )
        response.raise_for_status()
        found = response.json()["response"]
        return found["docs"], found["numFound"]

    return _pages(index, "records", fetch)


def _stac_copies(
    index: STACESGFIndex,
    mapping: _Mapping,
    query: Query,
) -> list[_Copy]:
    """Get the datasets on STAC that match a query, with their files."""
    project = query.get("project", [""])[0]
    collection = _stac_collection(index.url, project)
    if collection is None:
        return []
    copies = []
    for item in _stac_search(index, collection, project, query):
        properties = item.get("properties", {})
        dates = _file_ranges(item)
        copies.append(
            _Copy(
                index=index,
                id=str(item.get("id", "")),
                values=_values(
                    mapping,
                    lambda field, p=properties: p.get(
                        _stac_name(collection, project, field),
                    ),
                ),
                # Datasets without dates, e.g. of time-independent variables.
                dates=tuple(sorted(dates)) or (_ANY_DATES,),
                from_files=True,
            ),
        )
    return copies


def _stac_search(
    index: STACESGFIndex,
    collection: _Collection,
    project: str,
    query: Query,
) -> list[dict[str, Any]]:
    """Get the datasets on STAC that match a query, one page at a time.

    If there are too many, the first ones are returned, which is recorded as
    a failure.
    """
    url: str | None = f"https://{index.url}/search"
    params: dict[str, Any] | None = {
        "collections": collection.url.rsplit("/", 1)[-1],
        "limit": _PAGE_SIZE,
        "fields": "id,assets,properties",
        # Some indices ignore the filter of the items of a collection, but
        # not that of a search.
        **_stac_filter(collection, project, query),
    }
    items: list[dict[str, Any]] = []
    total = 0
    while url is not None and len(items) < _MAX_RECORDS:
        response = index.session.get(
            url,
            params=params,
            timeout=_RECORDS_TIMEOUT,
        )
        response.raise_for_status()
        page = response.json()
        items.extend(page["features"])
        total = page.get("numberMatched", len(items))
        links = [
            link for link in page.get("links", []) if link.get("rel") == "next"
        ]
        # The link to the next page has the parameters.
        url = links[0]["href"] if links and page["features"] else None
        params = None
        if len(items) >= total:
            break
    if url is not None and len(items) < total:
        logger.warning(
            "Only read the first %s of %s datasets on %s",
            len(items),
            total,
            index,
        )
        record_failure(
            index,
            f"found more datasets than the {len(items)} that were read",
        )
    return items


def _files(
    copies: Iterable[_Copy],
) -> dict[tuple[Index, str], list[tuple[str, str]]]:
    """Get the first and last date of the files of copies of datasets.

    Copies whose files cannot be found are left out.
    """
    ids: dict[Index, set[str]] = {}
    for copy in copies:
        ids.setdefault(copy.index, set()).add(copy.id)

    def search(
        job: tuple[Index, set[str]],
    ) -> dict[tuple[Index, str], list[tuple[str, str]]]:
        index, found = job
        files = _ask(
            index,
            "the names of the files",
            lambda: _index_files(index, sorted(found)),
            {},
        )
        return {(index, i): dates for i, dates in files.items()}

    with ThreadPoolExecutor(max_workers=max(len(ids), 1)) as pool:
        return {
            key: dates
            for found in pool.map(search, ids.items())
            for key, dates in found.items()
        }


def _index_files(
    index: Index,
    ids: list[str],
) -> dict[str, list[tuple[str, str]]]:
    """Get the first and last date of the files of datasets on an index.

    Datasets without files have no dates.
    """
    files: dict[str, list[tuple[str, str]]] = {i: [] for i in ids}
    size = (
        _GLOBUS_DATASETS_PER_REQUEST
        if isinstance(index, GlobusESGFIndex)
        else _SOLR_DATASETS_PER_REQUEST
    )
    for start in range(0, len(ids), size):
        batch = ids[start : start + size]
        if isinstance(index, GlobusESGFIndex):
            records = _globus_records(
                index,
                [
                    {"type": "match_any", "field_name": name, "values": values}
                    for name, values in (
                        ("dataset_id", batch),
                        ("type", ["File"]),
                    )
                ],
            )
        elif isinstance(index, SolrESGFIndex):
            records = _solr_records(
                index,
                [
                    *(("dataset_id", i) for i in batch),
                    ("type", "File"),
                    ("fields", "dataset_id,title"),
                    ("format", "application/solr+json"),
                ],
            )
        else:
            return {}
        for record in records:
            name = str(next(iter(_as_list(record.get("title"))), ""))
            match = _FILE_DATES.search(name)
            dataset = str(next(iter(_as_list(record.get("dataset_id"))), ""))
            files.setdefault(dataset, []).append(
                (match[1], match[2]) if match else _ANY_DATES,
            )
    return files
