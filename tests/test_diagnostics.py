from textwrap import dedent

from lsprotocol import types

from esmvaltool_language_server.diagnostics import recipe_diagnostics

RECIPE = dedent("""
    documentation:
      title: Example
      description: Example recipe.
      authors: [andela_bouwe]
    preprocessors:
      pp:
        regrid:
          target_grid: 1x1
          scheme: linear
    diagnostics:
      example:
        scripts: null
    """)


def test_valid_recipe() -> None:
    assert recipe_diagnostics(RECIPE) == []


def test_unknown_preprocessor() -> None:
    text = RECIPE.replace("regrid:", "regird:")
    (diagnostic,) = recipe_diagnostics(text)
    assert diagnostic.message == (
        "preprocessors > pp > regird: unknown preprocessor function. "
        "Did you mean 'regrid'?"
    )
    assert diagnostic.severity == types.DiagnosticSeverity.Error
    assert diagnostic.source == "esmvaltool"
    assert diagnostic.range == types.Range(
        start=types.Position(line=7, character=4),
        end=types.Position(line=7, character=11),
    )


def test_warning() -> None:
    text = RECIPE.replace(
        "scheme: linear", "scheme: linear\n      scheme: nearest"
    )
    (diagnostic,) = recipe_diagnostics(text)
    assert diagnostic.severity == types.DiagnosticSeverity.Warning
    assert diagnostic.range.start.line == 9
    assert "duplicate key" in diagnostic.message


def test_yaml_syntax_error() -> None:
    text = RECIPE.replace("target_grid: 1x1", "target_grid: [1x1")
    (diagnostic,) = recipe_diagnostics(text)
    assert diagnostic.message.startswith("invalid YAML: ")
    assert diagnostic.severity == types.DiagnosticSeverity.Error


def test_empty_recipe() -> None:
    (diagnostic,) = recipe_diagnostics("")
    assert diagnostic.message == "recipe: expected a mapping, got None."
    assert diagnostic.range == types.Range(
        start=types.Position(line=0, character=0),
        end=types.Position(line=0, character=1),
    )
