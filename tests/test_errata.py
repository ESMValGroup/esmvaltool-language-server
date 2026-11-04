from __future__ import annotations

import io
import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests
import requests_cache
import urllib3
import yaml
from lsprotocol import types
from requests.adapters import HTTPAdapter

from esmvaltool_language_server import errata

if TYPE_CHECKING:
    from pathlib import Path

CESM2 = "CMIP6.CMIP.NCAR.CESM2.historical.r1i1p1f1.Amon.tas.gn"
SETUP = {
    "vocabs": [
        {"key": "status", "project": None},
        {"key": "source_id", "project": "cmip6"},
        {"key": "experiment_id", "project": "cmip6"},
        {"key": "variable_id", "project": "cmip6"},
        {"key": "source", "project": "cmip7"},
    ],
}


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
    """Answer requests with the response for the path."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = {issue["uid"]: issue for issue in issues}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.fail = False
        self.answers_with_error = False

    def get(
        self,
        url: str,
        params: dict[str, str],
        **_: object,
    ) -> _Response:
        path = url.removeprefix(errata.URL)
        self.requests.append((path, params))
        if self.fail:
            msg = "no connection"
            raise requests.ConnectionError(msg)
        if self.answers_with_error:
            return _Response({}, status=500)
        if path == "/2/search/errata/setup":
            return _Response(SETUP)
        if path == "/2/search/errata":
            return _Response(
                {
                    "results": [
                        {"uid": issue["uid"], "status": issue["status"]}
                        for issue in self.issues.values()
                    ],
                },
            )
        return _Response({"issue": self.issues[params["uid"]]})


def _issue(
    uid: str = "u1",
    status: str = "new",
    datasets: tuple[str, ...] = (f"{CESM2}#20190308",),
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": f"Problem {uid}",
        "status": status,
        "datasets": list(datasets),
    }


def _diagnostics(
    text: str,
    recipe: object,
    service: errata.ErrataService,
) -> list[types.Diagnostic]:
    """Get the diagnostics after all errata have been looked up."""
    updates = list(errata.errata_diagnostics(text, recipe, service))
    return updates[-1] if updates else []


def _service(
    monkeypatch: pytest.MonkeyPatch,
    session: _Session,
) -> errata.ErrataService:
    """Create a service that gets its answers from the session."""
    monkeypatch.setattr(
        errata.ErrataService,
        "_open_session",
        lambda _: session,
    )
    return errata.ErrataService()


class _Adapter(HTTPAdapter):
    """Answer the requests of a real session with the fake session."""

    def __init__(self, session: _Session) -> None:
        super().__init__()
        self.session = session

    def send(
        self,
        request: requests.PreparedRequest,
        **_: object,
    ) -> requests.Response:
        url = urlsplit(str(request.url))
        answer = self.session.get(
            f"{errata.URL}{url.path}",
            dict(parse_qsl(url.query)),
        )
        raw = urllib3.HTTPResponse(
            body=io.BytesIO(json.dumps(answer.data).encode()),
            status=answer.status,
            preload_content=False,
        )
        return self.build_response(request, raw)


def _cached_service(
    path: Path,
    session: _Session,
    max_age: float = 3600,
) -> errata.ErrataService:
    """Create a service with a real cache on disk."""
    service = errata.ErrataService(cache=path / "errata", max_age=max_age)
    service.session.mount("https://", _Adapter(session))
    return service


def _recipe(facets: str) -> tuple[str, object]:
    text = (
        "datasets:\n"
        f"  - {{{facets}}}\n"
        "diagnostics:\n"
        "  diag:\n"
        "    variables:\n"
        "      tas: {mip: Amon, short_name: tas}\n"
    )
    return text, yaml.safe_load(text)


def test_service_searches_project_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_issue()])
    service = _service(monkeypatch, session)
    found = service.errata("CMIP6", "CESM2")
    assert [e.title for e in found] == ["Problem u1"]
    assert found[0].url == f"{errata.URL}/static/view.html?uid=u1"
    assert (
        "/2/search/errata",
        {"project": "cmip6", "source_id": "CESM2"},
    ) in (session.requests)


def test_service_remembers_results_on_disk(tmp_path: Path) -> None:
    session = _Session([_issue()])
    found = _cached_service(tmp_path, session).errata("CMIP6", "CESM2")
    assert (tmp_path / "errata.sqlite").exists()
    count = len(session.requests)
    assert _cached_service(tmp_path, session).errata("CMIP6", "CESM2") == found
    assert len(session.requests) == count


def test_service_asks_again_after_max_age(tmp_path: Path) -> None:
    session = _Session([_issue()])
    service = _cached_service(tmp_path, session, max_age=0.01)
    service.errata("CMIP6", "CESM2")
    count = len(session.requests)
    time.sleep(0.05)
    service.errata("CMIP6", "CESM2")
    assert len(session.requests) == 2 * count


def test_service_removes_old_answers(tmp_path: Path) -> None:
    session = _Session([_issue()])
    _cached_service(tmp_path, session, max_age=0.01).errata("CMIP6", "CESM2")
    time.sleep(0.05)
    service = _cached_service(tmp_path, session)
    assert isinstance(service.session, requests_cache.CachedSession)
    assert len(service.session.cache.responses) == 0


def test_service_does_not_remember_failures(tmp_path: Path) -> None:
    session = _Session([_issue()])
    session.answers_with_error = True
    service = _cached_service(tmp_path, session)
    assert service.errata("CMIP6", "CESM2") == []
    session.answers_with_error = False
    assert service.errata("CMIP6", "CESM2") != []


def test_service_works_without_cache(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "file").touch()
    session = _Session([_issue()])
    service = _cached_service(tmp_path / "file", session)
    assert not isinstance(service.session, requests_cache.CachedSession)
    assert [e.uid for e in service.errata("CMIP6", "CESM2")] == ["u1"]
    assert "Not caching errata" in caplog.text


def test_service_survives_failure_of_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_issue()])

    def fail(*_: object, **__: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(session, "get", fail)
    assert _service(monkeypatch, session).errata("CMIP6", "CESM2") == []


def test_service_ignores_unknown_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_issue()])
    service = _service(monkeypatch, session)
    assert service.errata("OBS6", "CESM2") == []


def test_service_ignores_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _Session([_issue()])
    session.fail = True
    service = _service(monkeypatch, session)
    assert service.errata("CMIP6", "CESM2") == []
    assert "Not asking the errata service" in caplog.text


@pytest.mark.parametrize(
    ("facets", "expected"),
    [
        ({"dataset": "CESM2", "exp": "historical"}, True),
        ({"dataset": "CESM2", "exp": "ssp585"}, False),
        ({"dataset": "CESM2", "exp": ["ssp585", "historical"]}, True),
        ({"dataset": "CESM2", "exp": "*"}, True),
        ({"dataset": "cesm2", "ensemble": "r1i1p1f1"}, True),
        ({"dataset": "CESM2", "ensemble": "r2i1p1f1"}, False),
        ({"dataset": "CESM2", "ensemble": "r(1:3)i1p1f1"}, True),
        ({"dataset": "CESM2", "ensemble": "r(2:3)i1p1f1"}, False),
        ({"dataset": "CESM2", "ensemble": "r(1:3)i(2:3)p1f1"}, False),
        ({"dataset": "CESM2", "ensemble": "r(1:3)i(1:3)p1f1"}, True),
        ({"dataset": "CESM2", "ensemble": ["r2i1p1f1", "r(1:3)i1p1f1"]}, True),
        ({"dataset": "CESM2", "version": "v20190308"}, True),
        ({"dataset": "CESM2", "version": "20190308"}, True),
        ({"dataset": "CESM2", "version": "v20200101"}, False),
        ({"dataset": "CESM2", "version": "latest"}, False),
    ],
)
def test_affects(facets: dict[str, object], *, expected: bool) -> None:
    names = {"dataset", "exp", "ensemble"}
    assert errata.affects(f"{CESM2}#20190308", facets, names) is expected


def test_affects_without_version() -> None:
    assert errata.affects(CESM2, {"version": "v1"}, set()) is False
    assert errata.affects(CESM2, {"dataset": "CESM2"}, {"dataset"})


def test_diagnostics_for_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    text, recipe = _recipe(
        "project: CMIP6, dataset: CESM2, exp: historical, "
        "ensemble: r1i1p1f1, grid: gn, version: v20190308",
    )
    session = _Session(
        [
            _issue("open", "onhold"),
            _issue("closed", "resolved"),
            _issue("other", datasets=("CMIP6.CMIP.NCAR.CESM2.ssp585.x#1",)),
        ],
    )
    service = _service(monkeypatch, session)
    found = _diagnostics(text, recipe, service)
    assert [d.message for d in found] == [
        "Problem open",
        "Problem closed",
    ]
    assert [d.severity for d in found] == [
        types.DiagnosticSeverity.Warning,
        types.DiagnosticSeverity.Information,
    ]
    assert found[0].range.start.line == 1
    assert found[0].range.start.character == 4
    assert found[0].code_description is not None
    assert found[0].code_description.href.endswith("view.html?uid=open")


def test_diagnostics_use_variable_group_facets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "diagnostics:\n"
        "  diag:\n"
        "    variables:\n"
        "      tas:\n"
        "        project: CMIP6\n"
        "        additional_datasets:\n"
        "          - {dataset: CESM2, exp: historical}\n"
        "          - {dataset: CESM2, exp: ssp585}\n"
    )
    service = _service(monkeypatch, _Session([_issue()]))
    found = _diagnostics(text, yaml.safe_load(text), service)
    assert [d.range.start.line for d in found] == [6]


@pytest.mark.parametrize(
    "facets",
    ["project: CMIP6, dataset: '*'", "project: OBS6, dataset: CESM2"],
)
def test_no_diagnostics_without_lookup(
    monkeypatch: pytest.MonkeyPatch,
    facets: str,
) -> None:
    text, recipe = _recipe(facets)
    session = _Session([_issue()])
    service = _service(monkeypatch, session)
    assert _diagnostics(text, recipe, service) == []
    assert not [r for r in session.requests if r[0] == "/2/search/errata"]


def test_no_diagnostics_for_invalid_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch, _Session([_issue()]))
    assert _diagnostics("a: [", {}, service) == []


def test_resolved_errata_need_a_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, recipe = _recipe("project: CMIP6, dataset: CESM2")
    session = _Session(
        [
            _issue("resolved", "resolved"),
            _issue("wontfix", "wontfix"),
            _issue("new", "new"),
        ],
    )
    service = _service(monkeypatch, session)
    found = _diagnostics(text, recipe, service)
    assert [d.message for d in found] == ["Problem wontfix", "Problem new"]


def test_resolved_erratum_needs_the_version_to_be_affected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, recipe = _recipe("project: CMIP6, dataset: CESM2, version: v1")
    service = _service(monkeypatch, _Session([_issue("r", "resolved")]))
    assert _diagnostics(text, recipe, service) == []


def test_errata_per_variable_group(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        "datasets:\n"
        "  - {project: CMIP6, dataset: CESM2, exp: historical}\n"
        "diagnostics:\n"
        "  diag:\n"
        "    variables:\n"
        "      tas: {mip: Amon}\n"
        "      pr: {mip: Amon}\n"
        "      ta: {mip: Amon}\n"
        "    additional_datasets:\n"
        "      - {project: CMIP6, dataset: CESM2, exp: historical}\n"
    )
    session = _Session(
        [
            _issue("some", datasets=(CESM2, CESM2.replace("tas", "ta"))),
            _issue("all", "wontfix"),
        ],
    )
    session.issues["all"]["datasets"] = [
        CESM2.replace(".tas.", f".{name}.") for name in ("tas", "pr", "ta")
    ]
    service = _service(monkeypatch, session)
    found = _diagnostics(text, yaml.safe_load(text), service)
    assert [(d.range.start.line, d.message) for d in found] == [
        (1, "Problem some (affects tas, ta)"),
        (1, "Problem all"),
        (9, "Problem some (affects tas, ta)"),
        (9, "Problem all"),
    ]


@pytest.mark.parametrize(
    ("text", "line"),
    [
        pytest.param(
            "diagnostics:\n"
            "  diag:\n"
            "    variables:\n"
            "      tas: &tas\n"
            "        project: CMIP6\n"
            "        additional_datasets:\n"
            "          - {dataset: CESM2}\n"
            "      pr:\n"
            "        <<: *tas\n",
            6,
            id="variable group",
        ),
        pytest.param(
            "defaults: &defaults\n"
            "  additional_datasets:\n"
            "    - {project: CMIP6, dataset: CESM2}\n"
            "diagnostics:\n"
            "  diag:\n"
            "    <<: *defaults\n"
            "    variables:\n"
            "      pr: {mip: Amon}\n",
            2,
            id="diagnostic",
        ),
        pytest.param(
            "first: &first\n"
            "  additional_datasets:\n"
            "    - {project: CMIP6, dataset: CESM2}\n"
            "second: &second\n"
            "  additional_datasets:\n"
            "    - {project: CMIP6, dataset: CESM2, exp: historical}\n"
            "diagnostics:\n"
            "  diag:\n"
            "    <<: [*first, *second]\n"
            "    variables:\n"
            "      pr: {mip: Amon}\n",
            2,
            id="first of several merged mappings",
        ),
        pytest.param(
            "defaults: &defaults\n"
            "  additional_datasets:\n"
            "    - {project: CMIP6, dataset: CESM2, exp: ssp585}\n"
            "diagnostics:\n"
            "  diag:\n"
            "    <<: *defaults\n"
            "    additional_datasets:\n"
            "      - {project: CMIP6, dataset: CESM2, exp: historical}\n"
            "    variables:\n"
            "      pr: {mip: Amon}\n",
            7,
            id="key that overrides a merged key",
        ),
    ],
)
def test_errata_of_merged_datasets(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    line: int,
) -> None:
    issue = _issue(datasets=(CESM2.replace(".tas.", ".pr."),))
    service = _service(monkeypatch, _Session([issue]))
    found = _diagnostics(text, yaml.safe_load(text), service)
    assert [d.range.start.line for d in found] == [line]


def test_erratum_of_aliased_datasets_is_shown_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "datasets: &datasets\n"
        "  - {project: CMIP6, dataset: CESM2}\n"
        "diagnostics:\n"
        "  first:\n"
        "    additional_datasets: *datasets\n"
        "    variables:\n"
        "      tas: {mip: Amon}\n"
    )
    service = _service(monkeypatch, _Session([_issue()]))
    found = _diagnostics(text, yaml.safe_load(text), service)
    assert [d.range.start.line for d in found] == [1]


@pytest.mark.parametrize(
    ("affected", "message"),
    [
        (("tas", "pr", "ta"), "Problem u1"),
        (("tas", "pr"), "Problem u1 (affects tas, pr)"),
    ],
)
def test_erratum_of_dataset_with_several_paths_is_merged(
    monkeypatch: pytest.MonkeyPatch,
    affected: tuple[str, ...],
    message: str,
) -> None:
    text = (
        "datasets: &datasets\n"
        "  - {project: CMIP6, dataset: CESM2}\n"
        "diagnostics:\n"
        "  first:\n"
        "    additional_datasets: *datasets\n"
        "    variables:\n"
        "      tas: {mip: Amon}\n"
        "  second:\n"
        "    additional_datasets: *datasets\n"
        "    variables:\n"
        "      pr: {mip: Amon}\n"
        "      ta: {mip: Amon}\n"
    )
    issue = _issue(
        datasets=tuple(CESM2.replace(".tas.", f".{v}.") for v in affected),
    )
    service = _service(monkeypatch, _Session([issue]))
    found = _diagnostics(text, yaml.safe_load(text), service)
    assert [(d.range.start.line, d.message) for d in found] == [(1, message)]


def _fetched(session: _Session) -> set[str]:
    return {
        params["uid"]
        for path, params in session.requests
        if path == "/2/publication/retrieve"
    }


@pytest.mark.parametrize(
    ("facets", "fetched"),
    [
        ("project: CMIP6, dataset: CESM2", {"new"}),
        ("project: CMIP6, dataset: CESM2, version: '*'", {"new"}),
        ("project: CMIP6, dataset: CESM2, version: v1", {"new", "resolved"}),
    ],
)
def test_resolved_errata_are_only_fetched_for_a_version(
    monkeypatch: pytest.MonkeyPatch,
    facets: str,
    fetched: set[str],
) -> None:
    text, recipe = _recipe(facets)
    session = _Session([_issue("new"), _issue("resolved", "resolved")])
    _diagnostics(text, recipe, _service(monkeypatch, session))
    assert _fetched(session) == fetched


def test_diagnostics_are_yielded_for_each_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "datasets:\n"
        "  - {project: CMIP6, dataset: CESM2}\n"
        "  - {project: CMIP6, dataset: MIROC6}\n"
        "  - {project: CMIP6, dataset: CESM2, exp: historical}\n"
        "diagnostics:\n"
        "  diag:\n"
        "    variables:\n"
        "      tas: {mip: Amon}\n"
    )
    session = _Session(
        [
            _issue("cesm2"),
            _issue("miroc", datasets=(CESM2.replace("CESM2", "MIROC6"),)),
        ],
    )
    service = _service(monkeypatch, session)
    updates = errata.errata_diagnostics(text, yaml.safe_load(text), service)
    assert [[d.range.start.line for d in found] for found in updates] == [
        [1, 3],
        [1, 3, 2],
    ]


def _searches(session: _Session) -> list[dict[str, str]]:
    return [
        params
        for path, params in session.requests
        if path == "/2/search/errata"
    ]


@pytest.mark.parametrize(
    ("facets", "experiments"),
    [
        ("project: CMIP6, dataset: CESM2", [None]),
        ("project: CMIP6, dataset: CESM2, exp: '*'", [None]),
        ("project: CMIP6, dataset: CESM2, exp: historical", ["historical"]),
        (
            "project: CMIP6, dataset: CESM2, exp: [historical, ssp585]",
            ["historical", "ssp585"],
        ),
        ("project: CMIP7, dataset: CESM2, exp: historical", [None]),
    ],
)
def test_search_for_experiments(
    monkeypatch: pytest.MonkeyPatch,
    facets: str,
    experiments: list[str | None],
) -> None:
    text, recipe = _recipe(facets)
    session = _Session([_issue()])
    _diagnostics(text, recipe, _service(monkeypatch, session))
    assert [
        search.get("experiment_id") for search in _searches(session)
    ] == experiments


def test_erratum_of_several_experiments_is_found_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, recipe = _recipe(
        "project: CMIP6, dataset: CESM2, exp: [historical, ssp585]",
    )
    session = _Session([_issue()])
    found = _diagnostics(text, recipe, _service(monkeypatch, session))
    assert len(found) == 1
    assert len(_fetched(session)) == 1


def test_variables_of_a_dataset_search_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "datasets:\n"
        "  - {project: CMIP6, dataset: CESM2}\n"
        "diagnostics:\n"
        "  diag:\n"
        "    variables:\n"
        "      tas: {mip: Amon}\n"
        "      pr: {mip: Amon}\n"
    )
    session = _Session([_issue()])
    service = _service(monkeypatch, session)
    _diagnostics(text, yaml.safe_load(text), service)
    assert len(_searches(session)) == 1


def test_erratum_that_cannot_be_fetched_keeps_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_issue("first"), _issue("bad"), _issue("last")])
    del session.issues["bad"]["title"]
    service = _service(monkeypatch, session)
    found = service.errata("CMIP6", "CESM2")
    assert [e.uid for e in found] == ["first", "last"]
    assert service.failed


def test_service_is_not_asked_again_if_it_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_issue()])
    session.fail = True
    service = _service(monkeypatch, session)
    assert service.errata("CMIP6", "CESM2") == []
    assert service.errata("CMIP6", "MIROC6") == []
    assert len(session.requests) == 1
    assert service.failed


def test_service_is_asked_again_after_some_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(errata, "RETRY_AFTER", 0)
    session = _Session([_issue()])
    session.fail = True
    service = _service(monkeypatch, session)
    assert service.errata("CMIP6", "CESM2") == []
    session.fail = False
    assert [e.uid for e in service.errata("CMIP6", "CESM2")] == ["u1"]


def test_service_keeps_errata_found_before_it_became_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_issue("first"), _issue("last")])
    get = session.get

    def fail_on_last(
        url: str,
        params: dict[str, str],
        **_: object,
    ) -> _Response:
        session.fail = params.get("uid") == "last"
        return get(url, params)

    monkeypatch.setattr(session, "get", fail_on_last)
    service = _service(monkeypatch, session)
    assert [e.uid for e in service.errata("CMIP6", "CESM2")] == ["first"]


def test_service_remembers_results_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_issue()])
    service = _service(monkeypatch, session)
    service.errata("CMIP6", "CESM2")
    count = len(session.requests)
    service.errata("CMIP6", "CESM2")
    assert len(session.requests) == count


@pytest.mark.parametrize(
    ("variable", "affected", "message"),
    [
        pytest.param("rtnt", "rlut", "Problem u1", id="input of derived"),
        pytest.param("rtnt", "rtnt", "Problem u1", id="derived on ESGF"),
        pytest.param("rtnt", "tos", None, id="unrelated variable"),
        pytest.param("unknown", "tos", "Problem u1", id="cannot be derived"),
    ],
)
def test_errata_of_derived_variables(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    affected: str,
    message: str | None,
) -> None:
    text = (
        "datasets:\n"
        "  - {project: CMIP6, dataset: CESM2}\n"
        "diagnostics:\n"
        "  diag:\n"
        "    variables:\n"
        f"      {variable}: {{mip: Amon, derive: true}}\n"
    )
    issue = _issue(datasets=(CESM2.replace(".tas.", f".{affected}."),))
    service = _service(monkeypatch, _Session([issue]))
    found = _diagnostics(text, yaml.safe_load(text), service)
    assert [d.message for d in found] == ([message] if message else [])


def test_variable_that_is_not_derived_needs_its_own_erratum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text, recipe = _recipe("project: CMIP6, dataset: CESM2")
    issue = _issue(datasets=(CESM2.replace(".tas.", ".rlut."),))
    service = _service(monkeypatch, _Session([issue]))
    assert _diagnostics(text, recipe, service) == []
