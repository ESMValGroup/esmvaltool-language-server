"""Tell ESMValTool recipes apart from other files named like recipes.

Other tools also use files named ``recipe.yaml``, for example conda-forge,
so files are recognized as recipes by their top-level keys.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from esmvaltool_language_server.schema import RecipeSchema

# A key at the start of a line, possibly quoted, followed by a colon.
_TOP_LEVEL_KEY = re.compile(
    r"""^(["']?)([^\s#:"'{}\[\]-][^:#]*?)\1\s*:(\s|$)"""
)


def top_level_keys(text: str) -> set[str]:
    """Find the top-level keys of a YAML document.

    The keys are found from the text, so they are also found in documents
    that are not valid YAML.

    Parameters
    ----------
    text:
        The YAML text.

    Returns
    -------
    :
        The keys.
    """
    return {
        match[2]
        for line in text.splitlines()
        if (match := _TOP_LEVEL_KEY.match(line))
    }


def is_recipe(text: str, schema: RecipeSchema) -> bool:
    """Check if a document is an ESMValTool recipe.

    Parameters
    ----------
    text:
        The YAML text of the document.
    schema:
        The recipe schema, which defines the top-level keys of recipes.

    Returns
    -------
    :
        Whether the document has a top-level key of recipes, or no top-level
        keys yet, as in a new recipe.
    """
    keys = top_level_keys(text)
    return not keys or bool(keys & set(schema.schema["properties"]))
