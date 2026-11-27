"""Parse YAML with the C parser of libyaml if it is available.

The C parser is about ten times faster than the parser of PyYAML, which makes
a difference for large recipes, because they are parsed after every change.
The data and nodes of the last few recipes are remembered, because several
features need them.
The data and nodes are shared, so they must not be modified.
"""

from __future__ import annotations

import copy
import dataclasses
import functools

import yaml

Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@dataclasses.dataclass(frozen=True)
class _Parsed:
    data: object
    root: yaml.Node | None


@functools.lru_cache(maxsize=8)
def _parse(text: str) -> _Parsed | yaml.YAMLError:
    """Parse a YAML document, or get the error if it is not valid YAML.

    The data are not built from the nodes, because that changes the nodes of
    mappings with ``<<`` keys.
    """
    try:
        data = yaml.load(text, Loader=Loader)  # noqa: S506
        root = yaml.compose(text, Loader=Loader)
    except yaml.YAMLError as exc:
        return exc
    return _Parsed(data, root)


def _parsed(text: str) -> _Parsed:
    result = _parse(text)
    if isinstance(result, yaml.YAMLError):
        # The error is remembered and shared between threads, so raise a
        # copy that each caller can add its traceback to.
        raise copy.copy(result).with_traceback(None)
    return result


def load(text: str) -> object:
    """Load the data in a YAML document.

    Parameters
    ----------
    text:
        The YAML text.

    Returns
    -------
    :
        The data, which must not be modified.

    Raises
    ------
    yaml.YAMLError
        If the text is not valid YAML.
    """
    return _parsed(text).data


def compose(text: str) -> yaml.Node | None:
    """Compose a YAML document into a tree of nodes.

    Parameters
    ----------
    text:
        The YAML text.

    Returns
    -------
    :
        The root node, which must not be modified, or None if the document
        is empty.

    Raises
    ------
    yaml.YAMLError
        If the text is not valid YAML.
    """
    return _parsed(text).root
