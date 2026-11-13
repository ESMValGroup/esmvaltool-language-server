"""Parse YAML with the C parser of libyaml if it is available.

The C parser is about ten times faster than the parser of PyYAML, which makes
a difference for large recipes, because they are parsed after every change.
"""

from __future__ import annotations

import yaml

Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load(text: str) -> object:
    """Load the data in a YAML document."""
    return yaml.load(text, Loader=Loader)  # noqa: S506


def compose(text: str) -> yaml.Node | None:
    """Compose a YAML document into a tree of nodes."""
    return yaml.compose(text, Loader=Loader)
