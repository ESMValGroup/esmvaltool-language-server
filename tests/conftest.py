from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from lsprotocol import types

if TYPE_CHECKING:
    from collections.abc import Callable

CURSOR = "|"


def _split_cursor(text: str) -> tuple[str, types.Position]:
    before, _, after = text.partition(CURSOR)
    lines = before.split("\n")
    position = types.Position(line=len(lines) - 1, character=len(lines[-1]))
    return before + after, position


@pytest.fixture
def split_cursor() -> Callable[[str], tuple[str, types.Position]]:
    """Remove the cursor, marked by `|`, from a text and get its position."""
    return _split_cursor
