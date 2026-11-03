from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from concurrent.futures import Future
from typing import TYPE_CHECKING

import pytest
import yaml
from lsprotocol import types
from pygls.lsp.client import LanguageClient
from pygls.workspace import Workspace

from esmvaltool_language_server import server

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

TIMEOUT = 60


@contextlib.asynccontextmanager
async def _client(
    published: asyncio.Queue[types.PublishDiagnosticsParams],
) -> AsyncGenerator[LanguageClient]:
    """Start the server and a client that collects the diagnostics."""
    client = LanguageClient("test-client", "0")

    @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def collect(params: types.PublishDiagnosticsParams) -> None:
        published.put_nowait(params)

    await client.start_io(sys.executable, "-m", "esmvaltool_language_server")
    try:
        await client.initialize_async(
            types.InitializeParams(capabilities=types.ClientCapabilities()),
        )
        client.initialized(types.InitializedParams())
        yield client
    finally:
        await client.shutdown_async(None)
        client.exit(None)
        await client.stop()


def _open(client: LanguageClient, uri: str, text: str) -> None:
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri=uri,
                language_id="yaml",
                version=1,
                text=text,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_server_publishes_diagnostics(tmp_path: Path) -> None:
    published: asyncio.Queue[types.PublishDiagnosticsParams] = asyncio.Queue()
    async with _client(published) as client:
        uri = (tmp_path / "recipe_test.yml").as_uri()
        _open(client, uri, "documentation: {}\n")
        params = await asyncio.wait_for(published.get(), TIMEOUT)
        assert params.uri == uri
        assert params.version == 1
        messages = [d.message for d in params.diagnostics]
        assert "recipe: missing required key 'diagnostics'." in messages

        client.text_document_did_close(
            types.DidCloseTextDocumentParams(
                text_document=types.TextDocumentIdentifier(uri=uri),
            ),
        )
        params = await asyncio.wait_for(published.get(), TIMEOUT)
        assert not params.diagnostics


@pytest.mark.asyncio
async def test_server_features(tmp_path: Path) -> None:
    published: asyncio.Queue[types.PublishDiagnosticsParams] = asyncio.Queue()
    async with _client(published) as client:
        uri = (tmp_path / "recipe_test.yml").as_uri()
        document = types.TextDocumentIdentifier(uri=uri)
        # The emoji is one character, but two UTF-16 code units, which the
        # client counts.
        text = (
            "preprocessors:\n"
            "  pp: {regrid: {target_grid: 1x1, scheme: linear}}\n"
            "diagnostics:\n"
            "  diag:\n"
            "    variables:\n"
            '      tas: {short_name: tas, alias: "🌍", preprocessor: pp}\n'
            "    scripts: null\n"
        )
        _open(client, uri, text)
        await asyncio.wait_for(published.get(), TIMEOUT)

        completion = await client.text_document_completion_async(
            types.CompletionParams(
                text_document=document,
                position=types.Position(line=5, character=58),
            ),
        )
        assert isinstance(completion, types.CompletionList)
        assert [item.label for item in completion.items] == ["pp"]

        result = await client.text_document_hover_async(
            types.HoverParams(
                text_document=document,
                position=types.Position(line=1, character=8),
            ),
        )
        assert result is not None
        assert isinstance(result.contents, types.MarkupContent)
        assert "regridding" in result.contents.value

        location = await client.text_document_definition_async(
            types.DefinitionParams(
                text_document=document,
                position=types.Position(line=5, character=57),
            ),
        )
        assert location == types.Location(
            uri=uri,
            range=types.Range(
                start=types.Position(line=1, character=2),
                end=types.Position(line=1, character=4),
            ),
        )


class _FacetSearch:
    """Return a future that has not finished."""

    def __init__(self) -> None:
        self.future: Future[list[str]] = Future()

    def submit(self, *_: object) -> Future[list[str]]:
        return self.future


class _NoFacetSearch:
    """Do not search the values of any facet."""

    def submit(self, *_: object) -> None:
        return None


def _exp_completion_server(
    search: _FacetSearch | _NoFacetSearch,
) -> server.RecipeLanguageServer:
    """Create a server with a recipe that needs the values of `exp`."""
    ls = server.RecipeLanguageServer()
    ls.protocol._workspace = Workspace(None)
    ls.protocol.client_capabilities = types.ClientCapabilities()
    uri = "file:///recipe_test.yml"
    text = (
        "diagnostics:\n"
        "  diag:\n"
        "    variables:\n"
        "      tas: {project: CMIP6, mip: Amon, exp: }\n"
    )
    ls.workspace.put_text_document(
        types.TextDocumentItem(
            uri=uri,
            language_id="yaml",
            version=1,
            text=text,
        ),
    )
    ls.recipes[uri] = yaml.safe_load(text)
    ls.facet_search = search  # type: ignore[assignment]
    return ls


def _exp_completion_params() -> types.CompletionParams:
    return types.CompletionParams(
        text_document=types.TextDocumentIdentifier(
            uri="file:///recipe_test.yml",
        ),
        position=types.Position(line=3, character=44),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [True, False])
async def test_completion_waits_for_facet_search(
    monkeypatch: pytest.MonkeyPatch,
    *,
    found: bool,
) -> None:
    monkeypatch.setattr(server, "FACET_SEARCH_SECONDS", 0.1)
    search = _FacetSearch()
    ls = _exp_completion_server(search)
    if found:
        asyncio.get_running_loop().call_later(
            0.01,
            search.future.set_result,
            ["historical"],
        )
    result = await server.completion(ls, _exp_completion_params())
    labels = [item.label for item in result.items]
    assert result.is_incomplete is not found
    assert (labels == ["historical"]) is found


@pytest.mark.asyncio
async def test_completion_without_facet_search() -> None:
    ls = _exp_completion_server(_NoFacetSearch())
    result = await server.completion(ls, _exp_completion_params())
    assert not result.is_incomplete


_ERRATUM = types.Diagnostic(
    range=types.Range(
        start=types.Position(line=1, character=2),
        end=types.Position(line=1, character=8),
    ),
    message="An erratum",
)

_ERRATA_RECIPE = (
    "datasets:\n  - {project: CMIP6, dataset: CESM2}\ndiagnostics: {}\n"
)


def _errata_server(
    monkeypatch: pytest.MonkeyPatch,
    release: threading.Event,
) -> tuple[server.RecipeLanguageServer, list[types.PublishDiagnosticsParams]]:
    """Create a server with a recipe and errata that wait for an event."""
    ls = server.RecipeLanguageServer()
    ls.protocol._workspace = Workspace(None)
    ls.workspace.put_text_document(
        types.TextDocumentItem(
            uri="file:///recipe_test.yml",
            language_id="yaml",
            version=1,
            text=_ERRATA_RECIPE,
        ),
    )
    published: list[types.PublishDiagnosticsParams] = []
    monkeypatch.setattr(
        ls,
        "text_document_publish_diagnostics",
        published.append,
    )

    def errata_diagnostics(*_: object) -> list[types.Diagnostic]:
        release.wait(TIMEOUT)
        return [_ERRATUM]

    monkeypatch.setattr(server, "errata_diagnostics", errata_diagnostics)
    return ls, published


async def _wait_for(published: list[types.PublishDiagnosticsParams]) -> None:
    for _ in range(TIMEOUT * 100):
        if published:
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_errata_are_published_after_the_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    ls, published = _errata_server(monkeypatch, release)
    task = asyncio.create_task(ls._check("file:///recipe_test.yml", 0))
    await _wait_for(published)
    assert len(published) == 1
    assert _ERRATUM not in published[0].diagnostics
    release.set()
    await task
    assert len(published) == 2
    assert published[1].version == 1
    assert published[1].diagnostics[-1] == _ERRATUM
    assert published[1].diagnostics[:-1] == published[0].diagnostics


@pytest.mark.asyncio
async def test_errata_are_not_published_for_changed_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    ls, published = _errata_server(monkeypatch, release)
    task = asyncio.create_task(ls._check("file:///recipe_test.yml", 0))
    await _wait_for(published)
    ls.workspace.put_text_document(
        types.TextDocumentItem(
            uri="file:///recipe_test.yml",
            language_id="yaml",
            version=2,
            text=_ERRATA_RECIPE,
        ),
    )
    release.set()
    await task
    assert len(published) == 1


@pytest.mark.asyncio
async def test_errata_can_be_turned_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    release.set()
    ls, published = _errata_server(monkeypatch, release)
    ls.errata_enabled = False
    await ls._check("file:///recipe_test.yml", 0)
    assert len(published) == 1


@pytest.mark.parametrize(
    ("options", "enabled"),
    [
        (None, True),
        ({}, True),
        ({"errata": True}, True),
        ({"errata": False}, False),
    ],
)
def test_initialization_option_turns_off_errata(
    options: dict[str, bool] | None,
    *,
    enabled: bool,
) -> None:
    ls = server.RecipeLanguageServer()
    params = types.InitializeParams(
        capabilities=types.ClientCapabilities(),
        initialization_options=options,
    )
    server.initialize(ls, params)
    assert ls.errata_enabled is enabled


@pytest.mark.asyncio
async def test_no_errata_for_other_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    release.set()
    ls, published = _errata_server(monkeypatch, release)
    ls.workspace.put_text_document(
        types.TextDocumentItem(
            uri="file:///recipe_test.yml",
            language_id="yaml",
            version=1,
            text="package:\n  name: example\n",
        ),
    )
    await ls._check("file:///recipe_test.yml", 0)
    assert len(published) == 1
    assert _ERRATUM not in published[0].diagnostics


def test_check_document_ignores_other_files() -> None:
    _, diagnostics = server._check_document("package:\n  name: example\n")
    assert diagnostics == []


@pytest.mark.asyncio
async def test_other_files_have_no_features() -> None:
    ls = server.RecipeLanguageServer()
    ls.protocol._workspace = Workspace(None)
    ls.protocol.client_capabilities = types.ClientCapabilities()
    uri = "file:///recipe.yaml"
    ls.workspace.put_text_document(
        types.TextDocumentItem(
            uri=uri,
            language_id="yaml",
            version=1,
            text="package:\n  name: example\nabout:\n  \n",
        ),
    )
    document = types.TextDocumentIdentifier(uri=uri)
    position = types.Position(line=3, character=2)
    result = await server.completion(
        ls,
        types.CompletionParams(text_document=document, position=position),
    )
    assert result.items == []
    hover = await server.hover_(
        ls,
        types.HoverParams(text_document=document, position=position),
    )
    assert hover is None
    location = server.definition_(
        ls,
        types.DefinitionParams(text_document=document, position=position),
    )
    assert location is None
