from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import pytest
from lsprotocol import types
from pygls.lsp.client import LanguageClient

if TYPE_CHECKING:
    from pathlib import Path

TIMEOUT = 60


@pytest.mark.asyncio
async def test_server_publishes_diagnostics(tmp_path: Path) -> None:
    client = LanguageClient("test-client", "0")
    published: asyncio.Queue[types.PublishDiagnosticsParams] = asyncio.Queue()

    @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def collect(params: types.PublishDiagnosticsParams) -> None:
        published.put_nowait(params)

    await client.start_io(sys.executable, "-m", "esmvaltool_language_server")
    try:
        await client.initialize_async(
            types.InitializeParams(capabilities=types.ClientCapabilities()),
        )
        client.initialized(types.InitializedParams())
        uri = (tmp_path / "recipe_test.yml").as_uri()
        client.text_document_did_open(
            types.DidOpenTextDocumentParams(
                text_document=types.TextDocumentItem(
                    uri=uri,
                    language_id="yaml",
                    version=1,
                    text="documentation: {}\n",
                ),
            ),
        )
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
    finally:
        await client.shutdown_async(None)
        client.exit(None)
        await client.stop()
