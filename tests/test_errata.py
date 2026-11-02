from __future__ import annotations

from typing import Any

import pytest
import requests
import yaml
from lsprotocol import types

from esmvaltool_language_server import errata

CESM2 = "CMIP6.CMIP.NCAR.CESM2.historical.r1i1p1f1.Amon.tas.gn"
SETUP = {
    "vocabs": [
        {"key": "status", "project": None},
        {"key": "source_id", "project": "cmip6"},
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
        if path == "/2/search/errata/setup":
            return _Response(SETUP)
        if path == "/2/search/errata":
            return _Response(
                {"results": [{"uid": uid} for uid in self.issues]},
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


def _service(
    monkeypatch: pytest.MonkeyPatch,
    session: _Session,
    max_age: float = 3600,
) -> errata.ErrataService:
    """Create a service that gets its answers from the session."""

    def get(_: object, url: str, **kwargs: dict[str, str]) -> _Response:
        return session.get(url, **kwargs)

    monkeypatch.setattr(requests.Session, "get", get)
    return errata.ErrataService(max_age=max_age)


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


def test_service_remembers_results(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session([_issue()])
    service = _service(monkeypatch, session)
    service.errata("CMIP6", "CESM2")
    count = len(session.requests)
    service.errata("cmip6", "cesm2")
    assert len(session.requests) == count


def test_service_asks_again_after_max_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_issue()])
    service = _service(monkeypatch, session, max_age=0)
    service.errata("CMIP6", "CESM2")
    count = len(session.requests)
    service.errata("CMIP6", "CESM2")
    assert len(session.requests) == 2 * count


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
    assert "Failed to find the errata" in caplog.text


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
        "ensemble: r1i1p1f1, grid: gn",
    )
    session = _Session(
        [
            _issue("open", "onhold"),
            _issue("closed", "resolved"),
            _issue("other", datasets=("CMIP6.CMIP.NCAR.CESM2.ssp585.x#1",)),
        ],
    )
    service = _service(monkeypatch, session)
    found = errata.errata_diagnostics(text, recipe, service)
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
    found = errata.errata_diagnostics(text, yaml.safe_load(text), service)
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
    assert errata.errata_diagnostics(text, recipe, service) == []
    assert not [r for r in session.requests if r[0] == "/2/search/errata"]


def test_no_diagnostics_for_invalid_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch, _Session([_issue()]))
    assert errata.errata_diagnostics("a: [", {}, service) == []
