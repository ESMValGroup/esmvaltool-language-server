"""Go to where a preprocessor, task, or diagnostic script is defined."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from lsprotocol import types

from esmvaltool_language_server import references
from esmvaltool_language_server.context import context_at

if TYPE_CHECKING:
    from esmvaltool_language_server.context import Path


def definition(
    text: str,
    position: types.Position,
    uri: str,
) -> types.Location | None:
    """Find the definition of what the value at a position refers to.

    Parameters
    ----------
    text:
        The YAML text of the recipe.
    position:
        The position of the cursor.
    uri:
        The URI of the recipe.

    Returns
    -------
    :
        The location of the preprocessor, the task, or the diagnostic script
        that the value refers to, or :obj:`None` if it is not found.
    """
    context = context_at(text, position.line, position.character)
    if context is None or context.kind == "key" or not context.word:
        return None
    if references.is_script(context.path):
        return _script_location(context.word)
    if references.is_preprocessor(context.path):
        targets: list[Path] = [("preprocessors", context.word)]
    elif references.is_ancestor(context.path):
        targets = references.task_paths(context.word)
    else:
        return None
    return _key_location(text, uri, targets)


def _script_location(name: str) -> types.Location | None:
    path = references.script_file(name)
    if path is None:
        return None
    start = types.Position(line=0, character=0)
    return types.Location(
        uri=path.as_uri(),
        range=types.Range(start=start, end=start),
    )


def _key_location(
    text: str,
    uri: str,
    targets: list[Path],
) -> types.Location | None:
    """Find the first of the keys at the target paths."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    for target in targets:
        if (node := _find_key(root, target)) is not None:
            return types.Location(
                uri=uri,
                range=types.Range(
                    start=types.Position(
                        line=node.start_mark.line,
                        character=node.start_mark.column,
                    ),
                    end=types.Position(
                        line=node.end_mark.line,
                        character=node.end_mark.column,
                    ),
                ),
            )
    return None


def _find_key(node: yaml.Node | None, path: Path) -> yaml.Node | None:
    """Find the node of the last key of a path."""
    key_node = None
    for part in path:
        if not isinstance(node, yaml.MappingNode):
            return None
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and key.value == part:
                key_node, node = key, value
                break
        else:
            return None
    return key_node
