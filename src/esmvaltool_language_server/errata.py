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
import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, cast

import requests
import yaml
from lsprotocol import types

from esmvaltool_language_server import cmor, esgf
from esmvaltool_language_server.schema import child, data_at

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from esmvaltool_language_server.context import Path

logger = logging.getLogger(__name__)

URL = "https://errata.esgf.io"

SOURCE = "ESGF errata"

# Stop waiting for the errata service after this many seconds.
TIMEOUT = 10

# The names of the source of a dataset in the projects of the errata service.
_SOURCE_FACETS = ("source_id", "source")

# Errata of datasets that may still be affected.
_OPEN = frozenset({"new", "onhold"})

# The prefix that ESMValCore adds to the version of a dataset on ESGF.
_VERSION_PREFIX = "v"


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
    """Find errata on the ESGF errata service and remember them.

    Parameters
    ----------
    session:
        The session to send requests with.
    max_age:
        Ask the service again after this many seconds.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        max_age: float = 3600,
    ) -> None:
        self.session = session or requests.Session()
        self.max_age = max_age
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, ...], tuple[float, object]] = {}

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
        response = self.session.get(
            f"{URL}{path}",
            params=params,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _source_facets(self) -> dict[str, str]:
        """Get the name of the source of datasets in each project."""

        def get() -> dict[str, str]:
            setup = self._get("/2/search/errata/setup")
            return {
                vocab["project"]: vocab["key"]
                for vocab in setup["vocabs"]
                if vocab.get("project") and vocab["key"] in _SOURCE_FACETS
            }

        return self._cached(("setup",), get)

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

    def errata(self, project: str, source: str) -> list[Erratum]:
        """Find the errata of the datasets of a source in a project.

        Parameters
        ----------
        project:
            The project, for example CMIP6.
        source:
            The source of the datasets, for example CESM2.

        Returns
        -------
        :
            The errata. Empty if the service does not know the project or
            cannot be reached.
        """
        project = project.lower()
        try:
            facet = self._source_facets().get(project)
            if facet is None:
                return []
            search = self._cached(
                ("search", project, source.lower()),
                lambda: self._get(
                    "/2/search/errata",
                    project=project,
                    **{facet: source},
                ),
            )
            uids = [result["uid"] for result in search["results"]]
            with ThreadPoolExecutor(max_workers=8) as pool:
                return list(pool.map(self._erratum, uids))
        except (requests.RequestException, LookupError, ValueError) as exc:
            logger.warning("Failed to find the errata of %s: %s", source, exc)
            return []


def _values(value: object) -> list[str] | None:
    """Get the names that a facet value selects, or None for any name."""
    values = value if isinstance(value, list) else [value]
    if not values or not all(cmor.is_name(v) for v in values):
        return None
    # Ranges of ensemble members are not expanded.
    if any("(" in str(v) for v in values):
        return None
    return [str(v).lower() for v in values]


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


def _applies(
    erratum: Erratum,
    facets: Mapping[object, object],
    names: set[str],
) -> bool:
    """Check if an erratum applies to the dataset with these facets.

    A resolved erratum usually no longer affects the latest version, which
    is used if the recipe does not pin a version, so it only applies to a
    dataset with a version.
    """
    if erratum.status == "resolved" and _values(facets.get("version")) is None:
        return False
    return any(affects(i, facets, names) for i in erratum.datasets)


def find_errata(
    recipe: object,
    service: ErrataService,
) -> list[tuple[Path, Erratum, list[str] | None]]:
    """Find the errata of the datasets in a recipe.

    Aliases and merge keys can make a dataset part of several paths. It is
    reported once, for all of its variables.

    Parameters
    ----------
    recipe:
        The recipe.
    service:
        The errata service.

    Returns
    -------
    :
        The path of each dataset with an erratum, the erratum, and the short
        names of the variables it applies to, or None if it applies to all
        variables of the dataset.
    """
    # The first path, the number of variables, and the variables that each
    # erratum applies to, for each dataset.
    paths: dict[int, Path] = {}
    totals: Counter[int] = Counter()
    applies: dict[tuple[int, Erratum], list[str]] = {}
    for path in _dataset_paths(recipe):
        dataset = id(data_at(recipe, path))
        paths.setdefault(dataset, path)
        all_variables = cmor.dataset_facets(path, recipe)
        totals[dataset] += len(all_variables)
        for facets in all_variables:
            project, source = facets.get("project"), facets.get("dataset")
            if not (cmor.is_name(project) and cmor.is_name(source)):
                continue
            names = esgf.intake_esgf_facets(str(project)) - {
                "project",
                "version",
            }
            for erratum in service.errata(str(project), str(source)):
                if _applies(erratum, facets, names):
                    variables = applies.setdefault((dataset, erratum), [])
                    variables.append(str(facets.get("short_name")))
    return [
        (
            paths[dataset],
            erratum,
            None if len(variables) == totals[dataset] else variables,
        )
        for (dataset, erratum), variables in applies.items()
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
    if variables is None:
        return erratum.title
    return f"{erratum.title} (affects {', '.join(dict.fromkeys(variables))})"


def errata_diagnostics(
    text: str,
    recipe: object,
    service: ErrataService,
) -> list[types.Diagnostic]:
    """Describe the errata of the datasets in a recipe as LSP diagnostics.

    Parameters
    ----------
    text:
        The YAML text of the recipe.
    recipe:
        The recipe.
    service:
        The errata service.

    Returns
    -------
    :
        A diagnostic at the line of each dataset with an erratum, a warning
        if the erratum is open and information otherwise.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return []
    lines = text.splitlines()
    diagnostics = []
    for path, erratum, variables in find_errata(recipe, service):
        node = _node_at(root, path)
        line, column = node.start_mark.line, node.start_mark.column
        end = len(lines[line].rstrip()) if line < len(lines) else column
        diagnostics.append(
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=line, character=column),
                    end=types.Position(line=line, character=max(end, column)),
                ),
                message=_message(erratum, variables),
                severity=types.DiagnosticSeverity.Warning
                if erratum.status in _OPEN
                else types.DiagnosticSeverity.Information,
                code="erratum",
                code_description=types.CodeDescription(href=erratum.url),
                source=SOURCE,
            ),
        )
    return diagnostics
