from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING

import pytest
from lsprotocol import types
from pygls.lsp.client import LanguageClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

TIMEOUT = 60


@contextlib.asynccontextmanager
async def _client(
    published: asyncio.Queue[types.PublishDiagnosticsParams],
) -> AsyncIterator[LanguageClient]:
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
