"""Language server for ESMValTool recipes."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING

from lsprotocol import types
from pygls.lsp.server import LanguageServer

if TYPE_CHECKING:
    from collections.abc import Mapping
    from concurrent.futures import Future

    from pygls.workspace import TextDocument

from esmvaltool_language_server import __version__, references
from esmvaltool_language_server.completion import Options, completions
from esmvaltool_language_server.definition import definition
from esmvaltool_language_server.detection import is_recipe
from esmvaltool_language_server.diagnostics import check_recipe
from esmvaltool_language_server.errata import ErrataService, errata_diagnostics
from esmvaltool_language_server.hover import hover
from esmvaltool_language_server.schema import MISSING, recipe_schema
from esmvaltool_language_server.search import FacetSearch, unused_facets

logger = logging.getLogger(__name__)

# Wait this long after the last change before checking a recipe, so it is not
# checked after every keystroke.
DEBOUNCE_SECONDS = 0.3

# Wait this long for the values of a facet to be found on ESGF before
# suggesting other values.
FACET_SEARCH_SECONDS = 5.0


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
        self.facet_search = FacetSearch()
        self.errata = ErrataService()
        # Looking up errata sends the datasets in recipes to the ESGF errata
        # service, so the user can turn it off.
        self.errata_enabled = True

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
            recipe, diagnostics = await loop.run_in_executor(
                None,
                _check_document,
                document.source,
            )
        except Exception:
            logger.exception("Failed to check recipe %s", uri)
            return
        if self.pending.get(uri) is asyncio.current_task():
            del self.pending[uri]
        if recipe is not MISSING:
            self.recipes[uri] = recipe
        lines = document.lines
        self._publish(document, lines, version, diagnostics)
        if recipe is not MISSING and self.errata_enabled:
            await self._publish_errata(document, lines, recipe, diagnostics)

    def _publish(
        self,
        document: TextDocument,
        lines: list[str],
        version: int | None,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        # Copy the diagnostics, because they are published more than once.
        converted = [copy.copy(diagnostic) for diagnostic in diagnostics]
        for diagnostic in converted:
            diagnostic.range = document.position_codec.range_to_client_units(
                lines,
                diagnostic.range,
            )
        self.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=document.uri,
                version=version,
                diagnostics=converted,
            ),
        )

    async def _publish_errata(
        self,
        document: TextDocument,
        lines: list[str],
        recipe: object,
        diagnostics: list[types.Diagnostic],
    ) -> None:
        """Publish the diagnostics again with the errata of the datasets.

        The errata are looked up after the diagnostics have been published,
        because the service may be slow. Nothing is published if there are no
        errata or the recipe has changed or was closed in the meantime.
        """
        uri, version = document.uri, document.version
        loop = asyncio.get_running_loop()
        try:
            errata = await loop.run_in_executor(
                None,
                errata_diagnostics,
                document.source,
                recipe,
                self.errata,
            )
        except Exception:
            logger.exception("Failed to find errata for %s", uri)
            return
        if not errata or uri not in self.workspace.text_documents:
            return
        if self.workspace.get_text_document(uri).version != version:
            return
        self._publish(document, lines, version, [*diagnostics, *errata])


def _check_document(text: str) -> tuple[object, list[types.Diagnostic]]:
    """Check a document, unless it is not a recipe."""
    if not is_recipe(text, recipe_schema()):
        return MISSING, []
    return check_recipe(text)


server = RecipeLanguageServer()


def _recipe_document(
    ls: RecipeLanguageServer, uri: str
) -> TextDocument | None:
    """Get a document, or None if it is not a recipe."""
    document = ls.workspace.get_text_document(uri)
    return document if is_recipe(document.source, recipe_schema()) else None


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


@server.feature(types.INITIALIZE)
def initialize(
    ls: RecipeLanguageServer,
    params: types.InitializeParams,
) -> None:
    """Read the settings that the editor sends when it starts the server."""
    options = params.initialization_options
    if isinstance(options, dict):
        ls.errata_enabled = options.get("errata", True) is not False


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
async def completion(
    ls: RecipeLanguageServer,
    params: types.CompletionParams,
) -> types.CompletionList:
    """Suggest keys and values."""
    uri = params.text_document.uri
    document = _recipe_document(ls, uri)
    if document is None:
        return types.CompletionList(is_incomplete=False, items=[])
    codec = document.position_codec
    pending: list[Future[list[str]]] = []

    def facet_values(
        project: str,
        facets: Mapping[object, object],
        facet: str,
    ) -> list[str] | None:
        future = ls.facet_search.submit(project, facets, facet)
        if future is None:
            return []
        if future.done():
            return future.result()
        pending.append(future)
        return None

    def complete() -> list[types.CompletionItem]:
        return completions(
            document.source,
            _position(document, params.position),
            ls.recipes.get(uri, MISSING),
            recipe_schema(),
            Options(
                snippets=_supports_snippets(ls.client_capabilities),
                facet_values=facet_values,
                unused_facets=unused_facets,
            ),
        )

    items = complete()
    if pending:
        await asyncio.wait(
            [asyncio.wrap_future(future) for future in pending],
            timeout=FACET_SEARCH_SECONDS,
        )
        pending.clear()
        items = complete()
    for item in items:
        if isinstance(item.text_edit, types.TextEdit):
            item.text_edit.range = codec.range_to_client_units(
                document.lines,
                item.text_edit.range,
            )
    # Ask the editor to request the suggestions again while the values of
    # facets are still being searched for.
    return types.CompletionList(is_incomplete=bool(pending), items=items)


def _supports_snippets(capabilities: types.ClientCapabilities) -> bool:
    document = capabilities.text_document
    completion = None if document is None else document.completion
    item = None if completion is None else completion.completion_item
    return bool(item and item.snippet_support)


@server.feature(types.TEXT_DOCUMENT_HOVER)
async def hover_(
    ls: RecipeLanguageServer,
    params: types.HoverParams,
) -> types.Hover | None:
    """Show the documentation of a key or a preprocessor."""
    uri = params.text_document.uri
    document = _recipe_document(ls, uri)
    if document is None:
        return None
    codec = document.position_codec
    # Describing a value may download documentation, so do not block the
    # server.
    result = await asyncio.to_thread(
        hover,
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
    document = _recipe_document(ls, uri)
    if document is None:
        return None
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
