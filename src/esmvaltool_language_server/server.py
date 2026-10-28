"""Language server for ESMValTool recipes."""

from __future__ import annotations

import asyncio
import logging

from lsprotocol import types
from pygls.lsp.server import LanguageServer

from esmvaltool_language_server import __version__
from esmvaltool_language_server.diagnostics import recipe_diagnostics

logger = logging.getLogger(__name__)

# Wait this long after the last change before checking a recipe, so it is not
# checked after every keystroke.
DEBOUNCE_SECONDS = 0.3


class RecipeLanguageServer(LanguageServer):
    """Language server that checks recipes."""

    def __init__(self) -> None:
        super().__init__(
            "esmvaltool-language-server",
            __version__,
            text_document_sync_kind=types.TextDocumentSyncKind.Full,
        )
        self.pending: dict[str, asyncio.Task[None]] = {}

    def schedule_check(self, uri: str, delay: float = 0.0) -> None:
        """Check a recipe after a delay, replacing an earlier check."""
        if task := self.pending.pop(uri, None):
            task.cancel()
        self.pending[uri] = asyncio.create_task(self._check(uri, delay))

    async def _check(self, uri: str, delay: float) -> None:
        await asyncio.sleep(delay)
        document = self.workspace.get_text_document(uri)
        version = document.version
        # Checking a large recipe takes up to a second, so do it in a thread
        # to keep the server responsive.
        loop = asyncio.get_running_loop()
        try:
            diagnostics = await loop.run_in_executor(
                None,
                recipe_diagnostics,
                document.source,
            )
        except Exception:
            logger.exception("Failed to check recipe %s", uri)
            return
        if self.pending.get(uri) is asyncio.current_task():
            del self.pending[uri]
        self.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=uri,
                version=version,
                diagnostics=diagnostics,
            ),
        )


server = RecipeLanguageServer()


@server.feature(types.INITIALIZED)
async def initialized(
    _ls: RecipeLanguageServer,
    _params: types.InitializedParams,
) -> None:
    """Prepare the recipe checks, so the first check is fast."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, recipe_diagnostics, "")


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(
    ls: RecipeLanguageServer,
    params: types.DidOpenTextDocumentParams,
) -> None:
    """Check a recipe when it is opened."""
    ls.schedule_check(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(
    ls: RecipeLanguageServer,
    params: types.DidChangeTextDocumentParams,
) -> None:
    """Check a recipe when the user stops typing."""
    ls.schedule_check(params.text_document.uri, DEBOUNCE_SECONDS)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(
    ls: RecipeLanguageServer,
    params: types.DidSaveTextDocumentParams,
) -> None:
    """Check a recipe when it is saved."""
    ls.schedule_check(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(
    ls: RecipeLanguageServer,
    params: types.DidCloseTextDocumentParams,
) -> None:
    """Remove the problems of a recipe when it is closed."""
    uri = params.text_document.uri
    if task := ls.pending.pop(uri, None):
        task.cancel()
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=[]),
    )
