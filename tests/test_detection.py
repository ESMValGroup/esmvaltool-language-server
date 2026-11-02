from __future__ import annotations

from textwrap import dedent

import pytest

from esmvaltool_language_server.detection import is_recipe, top_level_keys
from esmvaltool_language_server.schema import recipe_schema

# A conda-forge recipe for rattler-build.
CONDA_FORGE_RECIPE = dedent("""\
    context:
      version: "1.0"
    package:
      name: example
      version: ${{ version }}
    source:
      url: https://example.org/example-${{ version }}.tar.gz
    build:
      number: 0
    requirements:
      host:
        - python
    tests:
      - python:
          imports:
            - example
    about:
      license: Apache-2.0
    """)

# A conda-build recipe, which is not valid YAML.
CONDA_BUILD_RECIPE = dedent("""\
    {% set version = "1.0" %}
    package:
      name: example
      version: {{ version }}
    """)

# The input of the recipe filler of ESMValTool.
RECIPE_FILLER = dedent("""\
    ---
    recipe:
      - diagnostics:
          variables:
            variable: &variable
              project: CMIP6
    """)


def test_top_level_keys() -> None:
    text = dedent("""\
        # comment
        documentation:
          title: x
        "datasets": []
        'diagnostics' :
        - item
        not a key
        url: https://example.org
        """)
    assert top_level_keys(text) == {
        "documentation",
        "datasets",
        "diagnostics",
        "url",
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "---\n",
        "# A new recipe\n",
        "doc",
        "documentation:\n",
        "diagnostics: {}\n",
        "documentation:\n  title: x\nunknown_key: 1\n",
        "datasets: [\n",
    ],
)
def test_is_recipe(text: str) -> None:
    assert is_recipe(text, recipe_schema())


@pytest.mark.parametrize(
    "text",
    [CONDA_FORGE_RECIPE, CONDA_BUILD_RECIPE, RECIPE_FILLER],
)
def test_is_not_recipe(text: str) -> None:
    assert not is_recipe(text, recipe_schema())
