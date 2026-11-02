"""Find the values of facets of the data that ESMValCore can find.

The values are found with :meth:`esmvalcore.dataset.Dataset.from_files` in
the data sources that are configured for a project, except those that search
ESGF: the values on ESGF are found much faster by
:mod:`esmvaltool_language_server.esgf`.
The facets that can be searched for are the facets in the templates of the
paths of the data sources, for example of
:class:`esmvalcore.io.local.LocalDataSource`, and the facets that select the
time range, from the time ranges of the files.
"""

from __future__ import annotations

import functools
import re
from typing import TYPE_CHECKING, cast

from esmvalcore.config import CFG
from esmvalcore.dataset import Dataset
from esmvalcore.io import load_data_sources

from esmvaltool_language_server import timerange
from esmvaltool_language_server.cmor import is_name

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from esmvalcore.config import Session
    from esmvalcore.typing import FacetValue

_ESGF_DATA_SOURCES = frozenset(
    {
        "esmvalcore.io.esgf.ESGFDataSource",
        "esmvalcore.io.intake_esgf.IntakeESGFDataSource",
    },
)

# Attributes of data sources with templates of the paths of their data.
_TEMPLATES = ("dirname_template", "filename_template")

# A facet in a template, e.g. {dataset} or {dataset.lower}.
_TAG = re.compile(r"{([^}.]+)(?:\.(?:lower|upper))?}")

# Facets that :meth:`esmvalcore.dataset.Dataset.from_files` cannot find the
# values of.
_NOT_SEARCHABLE = frozenset({"project", "short_name"})


@functools.cache
def _session(project: str) -> Session | None:
    """Start a session without the data sources that search ESGF."""
    session = CFG.start_session("esmvaltool-language-server")
    settings = session["projects"].get(project, {})
    data = {
        name: source
        for name, source in settings.get("data", {}).items()
        if source.get("type") not in _ESGF_DATA_SOURCES
    }
    if not data:
        return None
    session["projects"] = {
        **session["projects"],
        project: {**settings, "data": data},
    }
    return session


@functools.cache
def known_facets(project: str) -> frozenset[str]:
    """Get the facets in the templates of the data sources of a project.

    Parameters
    ----------
    project:
        The project.

    Returns
    -------
    :
        The facets. Empty if the project has no data sources other than
        ESGF.
    """
    session = _session(project)
    if session is None:
        return frozenset()
    return frozenset(
        tag
        for source in load_data_sources(session, project)
        for name in _TEMPLATES
        if isinstance(template := getattr(source, name, None), str)
        for tag in _TAG.findall(template)
    )


def is_searchable(project: str, facet: str) -> bool:
    """Check if the values of a facet of a project can be found.

    Parameters
    ----------
    project:
        The project.
    facet:
        The facet.

    Returns
    -------
    :
        Whether the facet is in the templates of the data sources or selects
        the time range of the data.
    """
    if facet in timerange.FACETS:
        return _session(project) is not None
    return facet not in _NOT_SEARCHABLE and facet in known_facets(project)


def _is_value(value: object) -> bool:
    """Check if a facet value selects data, e.g. a name or a list of names."""
    values = value if isinstance(value, list) else [value]
    return bool(values) and all(
        is_name(v) or (isinstance(v, int) and not isinstance(v, bool))
        for v in values
    )


def _dataset(
    project: str,
    facets: Mapping[object, object],
    facet: str | None,
    session: Session,
) -> Dataset:
    """Create a dataset with any value of the facet and of missing facets."""
    selected: dict[str, FacetValue] = {
        str(key): cast("FacetValue", value)
        for key, value in facets.items()
        if key in known_facets(project) | {"short_name", "mip"}
        and _is_value(value)
    }
    # ESMValCore requires a dataset and mip to find data.
    dataset = Dataset(
        **{
            "dataset": "*",
            "mip": "*",
            **selected,
            "project": project,
            **({} if facet is None else {facet: "*"}),
        },
    )
    dataset.session = session
    # Use any value for the facets in the templates that are not given and
    # that ESMValCore does not add.
    augmented = dataset.copy()
    augmented.augment_facets()
    for name in known_facets(project) - set(augmented.facets):
        dataset.facets[name] = "*"
    return dataset


def facet_values(
    project: str,
    facets: Mapping[object, object],
    facet: str,
) -> list[str]:
    """Get the values of a facet of the data that matches facets.

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
        The values, sorted.
    """
    session = _session(project)
    if session is None or not is_searchable(project, facet):
        return []
    if facet in timerange.FACETS:
        ranges = _time_ranges(_dataset(project, facets, None, session))
        return timerange.suggestions(facet, ranges)
    dataset = _dataset(project, facets, facet, session)
    values = {
        str(found.facets[facet])
        for found in dataset.from_files()
        if is_name(str(found.facets.get(facet, "*")))
    }
    return sorted(values)


def _time_ranges(dataset: Dataset) -> Iterator[tuple[str, str]]:
    """Get the first and last date of the files of each matching dataset."""
    for found in dataset.from_files():
        dates = [
            str(file.facets["timerange"]).split("/")
            for file in found.files
            if "timerange" in file.facets
        ]
        if dates:
            yield min(d[0] for d in dates), max(d[-1] for d in dates)
