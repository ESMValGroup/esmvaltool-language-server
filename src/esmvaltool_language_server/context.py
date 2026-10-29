"""Find where the cursor is in a recipe.

A recipe is often not valid YAML while it is being edited, so the location is
found from the indentation of the lines above the cursor instead of by
parsing the recipe.
"""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Container

Path = tuple[str | int, ...]

# The start of a line in a block mapping or sequence: the indentation, the
# markers of sequence items, and a key.
_LINE = re.compile(
    r"(?P<indent> *)"
    r"(?P<dashes>(?:-(?: +|$))*)"
    r"(?:(?P<key>[^\s#'\"{}\[\],&*!|>%@`-][^#]*?|\"[^\"]*\"|'[^']*')"
    r" *:(?: +|$))?",
)

# Characters that separate the word at the cursor from the text around it.
_DELIMITERS = frozenset(" \t,:[]{}\"'")

_COMMENT = re.compile(r"(?:^|\s)#")

# Text in quotes, also if the closing quote has not been typed yet.
_QUOTED = re.compile(r"\"[^\"]*\"?|'[^']*'?")

# The characters that start, end, or separate items in flow collections.
_FLOW_TOKEN = re.compile(r"[\[\]{},]|:(?=\s|$)")


@dataclasses.dataclass(frozen=True)
class Context:
    """Location of the cursor in a recipe."""

    path: Path
    """Path of the mapping that contains the key, or of the value."""
    kind: Literal["key", "value", "item"]
    """Whether the cursor is at a key, a value, or the start of a sequence
    item, which can be a value or the first key of a mapping."""
    word: str
    """The word at the cursor."""
    start: int
    """Column where the word starts."""
    end: int
    """Column where the word ends."""
    flow: bool = False
    """Whether the cursor is in a flow collection, e.g. ``{a: 1, b: 2}``."""


@dataclasses.dataclass(frozen=True)
class _Line:
    indent: int
    dashes: tuple[int, ...]
    key: str | None
    key_column: int
    end: int


def _unquote(text: str) -> str:
    if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _parse_line(line: str) -> _Line | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped in {"---", "..."}:
        return None
    match = _LINE.match(line)
    if match is None:
        return None
    dashes = tuple(
        match.start("dashes") + i
        for i, char in enumerate(match["dashes"])
        if char == "-"
    )
    key = None if match["key"] is None else _unquote(match["key"].strip())
    key_column = match.start("key") if key is not None else match.end()
    return _Line(len(match["indent"]), dashes, key, key_column, match.end())


@dataclasses.dataclass
class _Parents:
    """The parents of an element, found from the lines above it."""

    limit: int
    """Column of the outermost element found so far."""
    is_item: bool
    """Whether that element is a sequence item."""
    path: list[str | int] = dataclasses.field(default_factory=list)
    """The path from that element to the element, in reverse order."""
    count: int = 0
    """The number of sequence items before that element."""

    def visit(self, line: _Line) -> None:
        """Visit the next line above the element."""
        if line.indent > self.limit:
            return
        if line.indent == self.limit:
            if self.is_item and line.dashes and line.dashes[0] == self.limit:
                self.count += 1
                return
            if not self.is_item:
                return
        tokens: list[tuple[int, str | None]] = [(d, None) for d in line.dashes]
        if line.key is not None:
            tokens.append((line.key_column, line.key))
        for column, key in reversed(tokens):
            if column < self.limit or (
                key is not None and self.is_item and column == self.limit
            ):
                self._add_parent(column, key)

    def _add_parent(self, column: int, key: str | None) -> None:
        if self.is_item:
            self.path.append(self.count)
        if key is None:
            self.is_item, self.count = True, 0
        else:
            self.path.append(key)
            self.is_item = False
        self.limit = column

    @property
    def done(self) -> bool:
        """Whether all parents have been found."""
        return self.limit == 0 and not self.is_item

    def result(self) -> list[str | int]:
        """Get the path of the element."""
        path = [*self.path, self.count] if self.is_item else self.path
        return path[::-1]


def _parent_path(
    lines: list[str],
    line_number: int,
    column: int,
    *,
    is_item: bool,
    skip: Container[int],
) -> list[str | int]:
    """Find the path of an element from the lines above it.

    The element starts at `column` and is a sequence item if `is_item`. The
    lines in `skip` are ignored.
    """
    parents = _Parents(column, is_item)
    for number in range(line_number - 1, -1, -1):
        if parents.done:
            break
        if number in skip:
            continue
        if (parsed := _parse_line(lines[number])) is not None:
            parents.visit(parsed)
    return parents.result()


def _balance(line: str) -> int:
    """Count the flow collections that a line opens minus those it closes."""
    masked = _QUOTED.sub(lambda match: "x" * len(match[0]), line)
    if comment := _COMMENT.search(masked):
        masked = masked[: comment.start()]
    return sum(masked.count(c) for c in "[{") - sum(
        masked.count(c) for c in "]}"
    )


def _flow_starts(lines: list[str], line_number: int) -> dict[int, int]:
    """Find the lines that continue a flow collection from an earlier line.

    Only the lines from the last line without indentation up to
    `line_number` are considered, which are the lines that can contain the
    parents of an element at `line_number`.

    Returns
    -------
    :
        For each line that continues a flow collection, the line where the
        collection starts.
    """
    top = line_number
    while top > 0 and not (lines[top][:1].strip() and lines[top][0] != "#"):
        top -= 1
    starts = {}
    depth = 0
    start = indent = top
    for number in range(top, line_number + 1):
        line = lines[number]
        if line.strip() and not line.lstrip().startswith("#"):
            # A collection ends where the indentation decreases, also if it
            # is not closed, so unbalanced brackets in text do not affect
            # the rest of the recipe.
            if depth > 0 and len(line) - len(line.lstrip()) < indent:
                depth = 0
            if depth == 0:
                start, indent = number, len(line) - len(line.lstrip())
        if depth > 0:
            starts[number] = start
        depth = max(depth + _balance(line), 0)
    return starts


@dataclasses.dataclass
class _Collection:
    """A flow collection that is open at the cursor."""

    bracket: str
    index: int = 0
    key: str | None = None


def _flow_path(text: str) -> tuple[list[str | int], bool] | None:
    """Find the path inside the flow collections that are open in `text`.

    Returns the path and whether the cursor is at a key.
    """
    # Hide quoted text, so the brackets and commas in it are ignored.
    masked = _QUOTED.sub(lambda match: "x" * len(match[0]), text)
    stack: list[_Collection] = []
    word_start = 0
    for match in _FLOW_TOKEN.finditer(masked):
        symbol = match[0]
        if symbol in "[{":
            stack.append(_Collection(symbol))
        elif symbol in "]}":
            del stack[-1:]
        elif stack and symbol == ",":
            stack[-1].index += 1
            stack[-1].key = None
        elif stack and stack[-1].bracket == "{":
            stack[-1].key = _unquote(text[word_start : match.start()].strip())
        word_start = match.end()
    return _open_collections_path(stack)


def _open_collections_path(
    stack: list[_Collection],
) -> tuple[list[str | int], bool] | None:
    if not stack:
        return [], False
    path: list[str | int] = []
    for collection in stack:
        if collection.bracket == "[":
            path.append(collection.index)
        elif collection.key is not None:
            path.append(collection.key)
        elif collection is not stack[-1]:
            return None
    return path, stack[-1].bracket == "{" and stack[-1].key is None


def _word(line: str, character: int) -> tuple[int, int]:
    start = character
    while start > 0 and line[start - 1] not in _DELIMITERS:
        start -= 1
    end = character
    while end < len(line) and line[end] not in _DELIMITERS:
        end += 1
    return start, end


def _is_key(line: str, end: int) -> bool:
    return line[end : end + 1] == ":" and line[end + 1 : end + 2] in {
        "",
        " ",
        "\t",
    }


def context_at(text: str, line_number: int, character: int) -> Context | None:
    """Find where the cursor is in a recipe.

    Parameters
    ----------
    text:
        The YAML text of the recipe.
    line_number:
        Line of the cursor, starting at 0.
    character:
        Column of the cursor, starting at 0.

    Returns
    -------
    :
        The location of the cursor, or :obj:`None` if it is in a comment or
        in text that is not understood.
    """
    lines = text.splitlines()
    while len(lines) <= line_number:
        lines.append("")
    line = lines[line_number]
    before = line[:character]
    if _COMMENT.search(before):
        return None
    # The text from the start of the flow collection that the cursor is in,
    # if it starts on an earlier line.
    starts = _flow_starts(lines, line_number)
    first = starts.get(line_number, line_number)
    head = lines[first] if first < line_number else before
    text_before = "\n".join([*lines[first:line_number], before])
    parsed = _parse_line(head) or _Line(len(head), (), None, 0, 0)
    if parsed.dashes:
        column, is_item = parsed.dashes[0], True
    elif parsed.key is not None:
        column, is_item = parsed.key_column, False
    else:
        column, is_item = len(head) - len(head.lstrip()), False
    path = _parent_path(lines, first, column, is_item=is_item, skip=starts)
    path.extend(0 for _ in parsed.dashes[1:])
    if parsed.key is not None:
        path.append(parsed.key)
    flow = _flow_path(text_before[parsed.end :])
    if flow is None:
        return None
    flow_path, at_key = flow
    path.extend(flow_path)
    start, end = _word(line, character)
    kind: Literal["key", "value", "item"]
    if at_key or (parsed.key is None and not parsed.dashes and not flow_path):
        kind = "key"
    elif flow_path or parsed.key is not None:
        kind = "value"
    else:
        kind = "item"
    if kind == "item" and _is_key(line, end):
        kind = "key"
    return Context(
        tuple(path),
        kind,
        line[start:end],
        start,
        end,
        flow=bool(flow_path) or at_key,
    )
