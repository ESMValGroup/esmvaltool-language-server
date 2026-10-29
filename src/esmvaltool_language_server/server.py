"""Language server for ESMValTool recipes."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from lsprotocol import types
from pygls.lsp.server import LanguageServer

if TYPE_CHECKING:
    from pygls.workspace import TextDocument

from esmvaltool_language_server import __version__, references
from esmvaltool_language_server.completion import completions
from esmvaltool_language_server.definition import definition
from esmvaltool_language_server.diagnostics import check_recipe
from esmvaltool_language_server.hover import hover
from esmvaltool_language_server.schema import MISSING, recipe_schema

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
        # The most recent version of each recipe that could be loaded, used
        # to decide which suggestions apply while the recipe is not valid
        # YAML.
        self.recipes: dict[str, object] = {}

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
        lines = document.lines
        try:
            recipe, diagnostics = await loop.run_in_executor(
                None,
                check_recipe,
                document.source,
            )
        except Exception:
            logger.exception("Failed to check recipe %s", uri)
            return
        if self.pending.get(uri) is asyncio.current_task():
            del self.pending[uri]
        if recipe is not MISSING:
            self.recipes[uri] = recipe
        for diagnostic in diagnostics:
            diagnostic.range = document.position_codec.range_to_client_units(
                lines,
                diagnostic.range,
            )
        self.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=uri,
                version=version,
                diagnostics=diagnostics,
            ),
        )


server = RecipeLanguageServer()


def _position(
    document: TextDocument,
    position: types.Position,
) -> types.Position:
    """Convert a position from the client, so it counts characters."""
    converted = document.position_codec.position_from_client_units(
        document.lines,
        position,
    )
    return types.Position(line=converted.line, character=converted.character)


@server.feature(types.INITIALIZED)
async def initialized(
    _ls: RecipeLanguageServer,
    _params: types.InitializedParams,
) -> None:
    """Prepare the recipe checks, so the first check is fast."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, check_recipe, "")
    await loop.run_in_executor(None, recipe_schema)
    await loop.run_in_executor(None, references.script_names)


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
    ls.recipes.pop(uri, None)
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=[]),
    )


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=["/"]),
)
def completion(
    ls: RecipeLanguageServer,
    params: types.CompletionParams,
) -> types.CompletionList:
    """Suggest keys and values."""
    uri = params.text_document.uri
    document = ls.workspace.get_text_document(uri)
    codec = document.position_codec
    items = completions(
        document.source,
        _position(document, params.position),
        ls.recipes.get(uri, MISSING),
        recipe_schema(),
        snippets=_supports_snippets(ls.client_capabilities),
    )
    for item in items:
        if isinstance(item.text_edit, types.TextEdit):
            item.text_edit.range = codec.range_to_client_units(
                document.lines,
                item.text_edit.range,
            )
    return types.CompletionList(is_incomplete=False, items=items)


def _supports_snippets(capabilities: types.ClientCapabilities) -> bool:
    document = capabilities.text_document
    completion = None if document is None else document.completion
    item = None if completion is None else completion.completion_item
    return bool(item and item.snippet_support)


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover_(
    ls: RecipeLanguageServer,
    params: types.HoverParams,
) -> types.Hover | None:
    """Show the documentation of a key or a preprocessor."""
    uri = params.text_document.uri
    document = ls.workspace.get_text_document(uri)
    codec = document.position_codec
    result = hover(
        document.source,
        _position(document, params.position),
        ls.recipes.get(uri, MISSING),
        recipe_schema(),
    )
    if result is not None and result.range is not None:
        result.range = codec.range_to_client_units(
            document.lines,
            result.range,
        )
    return result


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def definition_(
    ls: RecipeLanguageServer,
    params: types.DefinitionParams,
) -> types.Location | None:
    """Go to the definition of a preprocessor, task, or diagnostic script."""
    uri = params.text_document.uri
    document = ls.workspace.get_text_document(uri)
    codec = document.position_codec
    location = definition(
        document.source,
        _position(document, params.position),
        uri,
    )
    if location is not None and location.uri == uri:
        location.range = codec.range_to_client_units(
            document.lines,
            location.range,
        )
    return location
