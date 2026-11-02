"""Format recipes with :func:`esmvalcore.recipe.format_recipe`."""

from __future__ import annotations

import difflib
import re

import yaml
from esmvalcore.recipe import format_recipe
from lsprotocol import types

# The start of a comment: a ``#`` at the start of a line or after whitespace.
_COMMENT = re.compile(r"(?:^|(?<=\s))#", re.MULTILINE)


def has_comments(text: str) -> bool:
    """Check if a recipe contains comments.

    Parameters
    ----------
    text:
        The YAML text of the recipe.

    Returns
    -------
    :
        Whether the recipe contains a comment.

    Raises
    ------
    yaml.YAMLError
        If the text is not valid YAML.
    """
    # A ``#`` in a scalar, e.g. in a quoted string or a block scalar, does
    # not start a comment.
    scalars = [
        (token.start_mark.index, token.end_mark.index)
        for token in yaml.scan(text, Loader=yaml.SafeLoader)
        if isinstance(token, yaml.ScalarToken)
    ]
    return any(
        not any(start <= match.start() < end for start, end in scalars)
        for match in _COMMENT.finditer(text)
    )


def _position(lines: list[str], line: int) -> types.Position:
    """Get the position of the start of a line, or of the end of the text."""
    if line < len(lines) or not lines or lines[-1].endswith("\n"):
        return types.Position(line=line, character=0)
    return types.Position(line=len(lines) - 1, character=len(lines[-1]))


def formatting_edits(text: str) -> list[types.TextEdit]:
    """Get the edits that format a recipe.

    Only the lines that change are edited, so the editor keeps the position
    of the cursor in the other lines.

    Parameters
    ----------
    text:
        The YAML text of the recipe.

    Returns
    -------
    :
        The edits, with positions that count characters.

    Raises
    ------
    yaml.YAMLError
        If the text is not valid YAML.
    """
    old = text.splitlines(keepends=True)
    new = format_recipe(text).splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    return [
        types.TextEdit(
            range=types.Range(
                start=_position(old, start),
                end=_position(old, end),
            ),
            new_text="".join(new[new_start:new_end]),
        )
        for tag, start, end, new_start, new_end in matcher.get_opcodes()
        if tag != "equal"
    ]
