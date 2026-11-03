from __future__ import annotations

import asyncio
import contextlib
import sys
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


POPULATE_RECIPE = (
    "datasets:\n"
    "  - {dataset: '*', project: CMIP6}\n"
    "diagnostics:\n"
    "  diag:\n"
    "    variables:\n"
    '      tas: {mip: Amon, alias: "🌍"}\n'
)


@pytest.mark.asyncio
async def test_populate_datasets_lens(tmp_path: Path) -> None:
    published: asyncio.Queue[types.PublishDiagnosticsParams] = asyncio.Queue()
    async with _client(published) as client:
        uri = (tmp_path / "recipe_test.yml").as_uri()
        document = types.TextDocumentIdentifier(uri=uri)
        _open(client, uri, POPULATE_RECIPE)
        lenses = await client.text_document_code_lens_async(
            types.CodeLensParams(text_document=document),
        )
        assert lenses is not None
        assert [lens.range.start.line for lens in lenses] == [0]
        command = lenses[0].command
        assert command is not None
        assert command.command == server.POPULATE_DATASETS
        assert list(command.arguments or []) == [uri, ["datasets"]]


class _PopulateServer(server.RecipeLanguageServer):
    """A server that records the edits and messages it sends."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.protocol._workspace = Workspace(None)
        self.protocol.client_capabilities = types.ClientCapabilities()
        self.workspace.put_text_document(
            types.TextDocumentItem(
                uri="file:///recipe_test.yml",
                language_id="yaml",
                version=3,
                text=text,
            ),
        )
        self.edits: list[types.ApplyWorkspaceEditParams] = []
        self.messages: list[types.ShowMessageParams] = []

    async def workspace_apply_edit_async(
        self,
        params: types.ApplyWorkspaceEditParams,
    ) -> types.ApplyWorkspaceEditResult:
        self.edits.append(params)
        return types.ApplyWorkspaceEditResult(applied=True)

    def window_show_message(self, params: types.ShowMessageParams) -> None:
        self.messages.append(params)


class _DatasetSearch:
    """Find CESM2 for any search."""

    def submit(self, *_: object) -> Future[list[str]]:
        future: Future[list[str]] = Future()
        future.set_result(["CESM2"])
        return future


@pytest.mark.asyncio
async def test_populate_datasets() -> None:
    ls = _PopulateServer(POPULATE_RECIPE)
    ls.facet_search = _DatasetSearch()  # type: ignore[assignment]
    uri = "file:///recipe_test.yml"
    await server.populate_datasets(ls, uri, ["datasets"])
    assert not ls.messages
    [params] = ls.edits
    [change] = params.edit.document_changes or []
    assert isinstance(change, types.TextDocumentEdit)
    assert change.text_document.version == 3
    assert [edit.new_text for edit in change.edits] == [
        "{dataset: CESM2, project: CMIP6}",
    ]


@pytest.mark.asyncio
async def test_populate_datasets_fails() -> None:
    ls = _PopulateServer(POPULATE_RECIPE.replace("diagnostics", "other"))
    await server.populate_datasets(ls, "file:///recipe_test.yml", ["datasets"])
    assert not ls.edits
    [message] = ls.messages
    assert message.type == types.MessageType.Error
    assert "No variable groups" in message.message


@pytest.mark.parametrize(("line", "found"), [(0, True), (1, False)])
def test_populate_datasets_action(line: int, *, found: bool) -> None:
    ls = _PopulateServer(POPULATE_RECIPE)
    position = types.Position(line=line, character=0)
    actions = server.code_action(
        ls,
        types.CodeActionParams(
            text_document=types.TextDocumentIdentifier(
                uri="file:///recipe_test.yml",
            ),
            range=types.Range(start=position, end=position),
            context=types.CodeActionContext(diagnostics=[]),
        ),
    )
    commands = [action.command for action in actions if action.command]
    assert [c.command for c in commands] == (
        [server.POPULATE_DATASETS] if found else []
    )


@pytest.mark.asyncio
async def test_populate_datasets_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ls = _PopulateServer(POPULATE_RECIPE.replace("diagnostics", "other"))
    ls.protocol.client_capabilities = types.ClientCapabilities(
        window=types.WindowClientCapabilities(work_done_progress=True),
    )
    progress = ls.work_done_progress
    reported: list[object] = []

    async def create_async(token: str) -> None:
        reported.append(token)

    monkeypatch.setattr(progress, "create_async", create_async)
    monkeypatch.setattr(progress, "begin", lambda _, v: reported.append(v))
    monkeypatch.setattr(progress, "end", lambda _, v: reported.append(v))
    await server.populate_datasets(ls, "file:///recipe_test.yml", ["datasets"])
    assert [type(value) for value in reported[1:]] == [
        types.WorkDoneProgressBegin,
        types.WorkDoneProgressEnd,
    ]


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


def test_other_files_have_no_populate_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ls = _PopulateServer(POPULATE_RECIPE)
    monkeypatch.setattr(server, "is_recipe", lambda *_: False)
    document = types.TextDocumentIdentifier(uri="file:///recipe_test.yml")
    lenses = server.code_lens(ls, types.CodeLensParams(text_document=document))
    assert lenses == []
    position = types.Position(line=2, character=0)
    actions = server.code_action(
        ls,
        types.CodeActionParams(
            text_document=document,
            range=types.Range(start=position, end=position),
            context=types.CodeActionContext(diagnostics=[]),
        ),
    )
    assert actions == []
