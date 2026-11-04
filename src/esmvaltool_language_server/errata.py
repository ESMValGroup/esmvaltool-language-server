"""Find errata of the datasets in a recipe on the ESGF errata service.

The `ESGF errata service <https://errata.esgf.io>`_ describes known problems
with datasets on ESGF and lists the affected datasets.
The errata of a dataset are found by searching for the errata of its project
and source, for example CMIP6 and CESM2, and comparing the facets of the
dataset with the identifiers of the affected datasets.
The facets that are compared are those in the configuration of the
intake-esgf data source of ESMValCore, because these are part of the
identifiers of datasets on ESGF.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import re
import sqlite3
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING, Any, cast

import requests
import requests_cache
import yaml
from esmvalcore.preprocessor._derive import get_required
from lsprotocol import types

from esmvaltool_language_server import cmor, esgf
from esmvaltool_language_server.schema import child, data_at

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from pathlib import Path as FilePath

    from esmvaltool_language_server.context import Path

logger = logging.getLogger(__name__)

URL = "https://errata.esgf.io"

SOURCE = "ESGF errata"

# Stop waiting for the errata service after this many seconds.
TIMEOUT = 10

# The location of the cache of answers of the service, in the user's cache
# directory, and how many seconds to keep them.
CACHE = "esmvaltool-language-server/errata"
MAX_AGE = 24 * 60 * 60

# Do not ask the service again for this many seconds if it cannot be reached.
RETRY_AFTER = 60

# The names of the source of a dataset in the projects of the errata service.
_SOURCE_FACETS = ("source_id", "source")

# The names of the experiment of a dataset in the projects of the errata
# service. The service finds no errata if it does not know a facet, so only
# facets that it lists for a project are used.
_EXPERIMENT_FACETS = ("experiment_id", "experiment")

# How to show the status of an erratum, if it differs from its name.
_STATUS_LABELS = {"onhold": "on hold", "wontfix": "won't fix"}

# The prefix that ESMValCore adds to the version of a dataset on ESGF.
_VERSION_PREFIX = "v"

# A range of ensemble members or start dates in a facet value.
_RANGE = re.compile(r"\((\d+):(\d+)\)")


class _UnavailableError(requests.RequestException):
    """The errata service cannot be reached."""


# What can go wrong when asking the service for an answer.
_FAILURES = (requests.RequestException, LookupError, ValueError, sqlite3.Error)


@dataclasses.dataclass(frozen=True)
class Erratum:
    """A known problem with datasets on ESGF."""

    uid: str
    """The identifier of the erratum."""
    title: str
    """The title."""
    status: str
    """The status, for example ``new`` or ``resolved``."""
    datasets: tuple[str, ...]
    """The identifiers of the affected datasets, with their version."""

    @property
    def url(self) -> str:
        """The web page of the erratum."""
        return f"{URL}/static/view.html?uid={self.uid}"


class ErrataService:
    """Find errata on the ESGF errata service and remember them on disk.

    Parameters
    ----------
    cache:
        The location of the cache, relative to the user's cache directory.
    max_age:
        Ask the service again after this many seconds.
    """

    def __init__(
        self,
        cache: str | FilePath = CACHE,
        max_age: float = MAX_AGE,
    ) -> None:
        self.cache = cache
        self.max_age = max_age
        self.failed = False
        """Whether asking the service failed, set until it is reset."""
        self._retry_at = 0.0
        # Reading the answers from disk takes too long to do for every check
        # of a recipe, so also remember them in memory.
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, ...], tuple[float, object]] = {}

    @functools.cached_property
    def session(self) -> requests.Session:
        """The session to send requests with, created when it is needed."""
        return self._open_session()

    def _open_session(self) -> requests.Session:
        try:
            session = requests_cache.CachedSession(
                str(self.cache),
                use_cache_dir=True,
                expire_after=self.max_age,
            )
            session.cache.delete(expired=True)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("Not caching errata in %s: %s", self.cache, exc)
            return requests.Session()
        return session

    def _cached[T](self, key: tuple[str, ...], get: Callable[[], T]) -> T:
        """Get a value from the cache, or get it and remember it."""
        now = time.monotonic()
        with self._lock:
            found = self._cache.get(key)
        if found is not None and now - found[0] < self.max_age:
            return cast("T", found[1])
        value = get()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def _get(self, path: str, **params: str) -> dict[str, Any]:
        if time.monotonic() < self._retry_at:
            self.failed = True
            msg = "The errata service cannot be reached"
            raise _UnavailableError(msg)
        try:
            response = self.session.get(
                f"{URL}{path}",
                params=params,
                timeout=TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            self.failed = True
            self._retry_at = time.monotonic() + RETRY_AFTER
            logger.warning(
                "Not asking the errata service for %s seconds: %s",
                RETRY_AFTER,
                exc,
            )
            raise _UnavailableError(str(exc)) from exc
        response.raise_for_status()
        return response.json()

    def _project_facets(self) -> dict[str, set[str]]:
        """Get the facets that can be searched for in each project."""

        def get() -> dict[str, set[str]]:
            facets: dict[str, set[str]] = {}
            for vocab in self._get("/2/search/errata/setup")["vocabs"]:
                if vocab.get("project"):
                    facets.setdefault(vocab["project"], set()).add(
                        vocab["key"],
                    )
            return facets

        return self._cached(("setup",), get)

    def _search(
        self,
        project: str,
        params: dict[str, str],
    ) -> list[dict[str, Any]]:
        def get() -> list[dict[str, Any]]:
            search = self._get("/2/search/errata", project=project, **params)
            return search["results"]

        key = ("search", project, *sorted(params.values()))
        return self._cached(key, get)

    def _erratum(self, uid: str) -> Erratum:
        def get() -> Erratum:
            issue = self._get("/2/publication/retrieve", uid=uid)["issue"]
            return Erratum(
                uid=uid,
                title=issue["title"],
                status=issue["status"],
                datasets=tuple(issue["datasets"]),
            )

        return self._cached(("erratum", uid), get)

    def _errata(self, uids: Iterable[str]) -> list[Erratum]:
        """Get the errata that can be found, if not all of them can."""
        found = []
        for uid in uids:
            try:
                found.append(self._erratum(uid))
            except _UnavailableError:
                break
            except _FAILURES as exc:
                self.failed = True
                logger.warning("Failed to get erratum %s: %s", uid, exc)
        return found

    def errata(
        self,
        project: str,
        source: str,
        *,
        experiments: tuple[str, ...] | None = None,
        resolved: bool = True,
    ) -> list[Erratum]:
        """Find the errata of the datasets of a source in a project.

        Parameters
        ----------
        project:
            The project, for example CMIP6.
        source:
            The source of the datasets, for example CESM2.
        experiments:
            Only find errata of these experiments, if the service can search
            for experiments in the project. None for all experiments.
        resolved:
            Whether to include resolved errata. Getting an erratum takes a
            request, and the search already tells which ones are resolved.

        Returns
        -------
        :
            The errata that could be found. Empty if the service does not know
            the project or cannot be reached.
        """
        project = project.lower()
        try:
            facets = self._project_facets().get(project, set())
            facet = next((f for f in _SOURCE_FACETS if f in facets), None)
            if facet is None:
                return []
            experiment = next(
                (f for f in _EXPERIMENT_FACETS if f in facets),
                None,
            )
            searches = (
                [{experiment: name} for name in experiments]
                if experiment and experiments
                else [{}]
            )
            uids = {
                result["uid"]: None
                for search in searches
                for result in self._search(project, {facet: source, **search})
                if resolved or result["status"] != "resolved"
            }
            return self._errata(uids)
        except _UnavailableError:
            return []
        except _FAILURES as exc:
            self.failed = True
            logger.warning("Failed to find the errata of %s: %s", source, exc)
            return []


@functools.lru_cache(maxsize=1024)
def _expand_ranges(value: str) -> tuple[str, ...]:
    """Expand ranges, for example ``r(1:2)i1p1f1``, like ESMValCore does."""
    match = _RANGE.search(value)
    if match is None:
        return (value,)
    start, end = int(match[1]), int(match[2])
    return tuple(
        name
        for i in range(start, end + 1)
        for name in _expand_ranges(
            f"{value[: match.start()]}{i}{value[match.end() :]}",
        )
    )


def _values(value: object) -> list[str] | None:
    """Get the names that a facet value selects, or None for any name."""
    values = value if isinstance(value, list) else [value]
    if not values or not all(cmor.is_name(v) for v in values):
        return None
    return [
        name.lower() for value in values for name in _expand_ranges(str(value))
    ]


def affects(
    dataset_id: str,
    facets: Mapping[object, object],
    names: set[str],
) -> bool:
    """Check if a dataset on ESGF is one of the datasets in a recipe.

    Parameters
    ----------
    dataset_id:
        The identifier of the dataset on ESGF, with its version, for example
        ``CMIP6.CMIP.NCAR.CESM2.historical.r1i1p1f1.Amon.tas.gn#20190308``.
    facets:
        The facets of the dataset in the recipe.
    names:
        The facets to compare with the parts of the identifier.

    Returns
    -------
    :
        Whether all facets of the dataset in the recipe that are names occur
        in the identifier, ignoring case.
    """
    identifier, _, version = dataset_id.partition("#")
    parts = {part.lower() for part in identifier.split(".")}
    if not version:
        version = identifier.rsplit(".", 1)[-1]
    for facet in names:
        values = _values(facets.get(facet))
        if values is not None and not parts & set(values):
            return False
    versions = _values(facets.get("version"))
    return versions is None or version.lower().removeprefix(
        _VERSION_PREFIX,
    ) in {v.removeprefix(_VERSION_PREFIX) for v in versions}


def _input_facets(
    facets: dict[object, object],
) -> list[Mapping[object, object]]:
    """Get the facets of the datasets on ESGF that a variable is made from.

    A derived variable is computed from other variables, or read from the
    variable itself if it is available.
    """
    if not facets.get("derive"):
        return [facets]
    try:
        required = get_required(
            str(facets.get("short_name")),
            str(facets.get("project")),
        )
    except NotImplementedError:
        return [{k: v for k, v in facets.items() if k != "short_name"}]
    return [facets, *({**facets, **facet} for facet in required)]


def _dataset_paths(recipe: object) -> Iterator[Path]:
    """Get the paths of the datasets in a recipe."""
    for i, _ in enumerate(_list(child(recipe, "datasets"))):
        yield ("datasets", i)
    diagnostics = child(recipe, "diagnostics")
    for name in diagnostics if isinstance(diagnostics, dict) else []:
        diagnostic = child(diagnostics, name)
        for i, _ in enumerate(_list(child(diagnostic, "additional_datasets"))):
            yield ("diagnostics", str(name), "additional_datasets", i)
        variables = child(diagnostic, "variables")
        for group in variables if isinstance(variables, dict) else []:
            datasets = child(child(variables, group), "additional_datasets")
            for i, _ in enumerate(_list(datasets)):
                yield (
                    "diagnostics",
                    str(name),
                    "variables",
                    str(group),
                    "additional_datasets",
                    i,
                )


def _list(data: object) -> list[object]:
    return data if isinstance(data, list) else []


def find_errata(
    recipe: object,
    service: ErrataService,
) -> Iterator[list[tuple[Path, Erratum, list[str] | None]]]:
    """Find the errata of the datasets in a recipe.

    Aliases and merge keys can make a dataset part of several paths. It is
    reported once, for all of its variables.

    A resolved erratum usually no longer affects the latest version, which
    is used if the recipe does not pin a version, so it is only looked up for
    a dataset with a version.

    Parameters
    ----------
    recipe:
        The recipe.
    service:
        The errata service.

    Yields
    ------
    :
        After looking up the errata of each source, all errata found so far:
        the path of each dataset with an erratum, the erratum, and the short
        names of the variables it applies to, or None if it applies to all
        variables of the dataset.
    """
    # The first path and the number of variables of each dataset, and the
    # variables to look up for each project and source.
    paths: dict[int, Path] = {}
    totals: Counter[int] = Counter()
    lookups: dict[
        tuple[str, str],
        list[tuple[int, dict[object, object]]],
    ] = {}
    for path in _dataset_paths(recipe):
        dataset = id(data_at(recipe, path))
        paths.setdefault(dataset, path)
        for facets in cmor.dataset_facets(path, recipe):
            totals[dataset] += 1
            project, source = facets.get("project"), facets.get("dataset")
            if cmor.is_name(project) and cmor.is_name(source):
                key = (str(project), str(source))
                lookups.setdefault(key, []).append((dataset, facets))
    # The variables that each erratum applies to, for each dataset.
    applies: dict[tuple[int, Erratum], list[str]] = {}
    # The variables of a dataset usually need the same errata.
    find = functools.cache(service.errata)
    for (project, source), variables in lookups.items():
        names = esgf.intake_esgf_facets(project) - {"project", "version"}
        for dataset, facets in variables:
            pinned = _values(facets.get("version")) is not None
            experiments = _values(facets.get("exp"))
            for erratum in find(
                project,
                source,
                experiments=tuple(experiments) if experiments else None,
                resolved=pinned,
            ):
                if any(
                    affects(identifier, inputs, names)
                    for inputs in _input_facets(facets)
                    for identifier in erratum.datasets
                ):
                    affected = applies.setdefault((dataset, erratum), [])
                    affected.append(str(facets.get("short_name")))
        yield [
            (
                paths[dataset],
                erratum,
                None if len(affected) == totals[dataset] else affected,
            )
            for (dataset, erratum), affected in applies.items()
        ]


def _node_at(root: yaml.Node, path: Path) -> yaml.Node:
    """Find the node of the data at a path in the document with that data."""
    loader = yaml.SafeLoader("")
    node = root
    for part in path:
        if isinstance(part, int):
            node = node.value[part]
        else:
            # Like when loading a document, the last key takes precedence and
            # merged keys come before the other keys.
            loader.flatten_mapping(node)
            node = next(v for k, v in reversed(node.value) if k.value == part)
    return node


def _message(erratum: Erratum, variables: list[str] | None) -> str:
    details = [_STATUS_LABELS.get(erratum.status, erratum.status)]
    if variables is not None:
        details.append(f"affects {', '.join(dict.fromkeys(variables))}")
    return f"{erratum.title} ({', '.join(details)})"


def _diagnostic(
    node: yaml.Node,
    lines: list[str],
    erratum: Erratum,
    variables: list[str] | None,
) -> types.Diagnostic:
    line, column = node.start_mark.line, node.start_mark.column
    end = len(lines[line].rstrip()) if line < len(lines) else column
    return types.Diagnostic(
        range=types.Range(
            start=types.Position(line=line, character=column),
            end=types.Position(line=line, character=max(end, column)),
        ),
        message=_message(erratum, variables),
        # An erratum is not a problem with the recipe, so it should stand out
        # from the errors and warnings that are.
        severity=types.DiagnosticSeverity.Information,
        code="erratum",
        code_description=types.CodeDescription(href=erratum.url),
        source=SOURCE,
    )


def errata_diagnostics(
    text: str,
    recipe: object,
    service: ErrataService,
) -> Iterator[list[types.Diagnostic]]:
    """Describe the errata of the datasets in a recipe as LSP diagnostics.

    Parameters
    ----------
    text:
        The YAML text of the recipe.
    recipe:
        The recipe.
    service:
        The errata service.

    Yields
    ------
    :
        After looking up the errata of each source, an information diagnostic
        at the line of each dataset with an erratum found so far.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return
    lines = text.splitlines()
    for found in find_errata(recipe, service):
        yield [
            _diagnostic(_node_at(root, path), lines, erratum, variables)
            for path, erratum, variables in found
        ]
