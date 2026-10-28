"""Find problems in recipes and describe them as LSP diagnostics."""

from __future__ import annotations

import yaml
from esmvalcore.recipe import RecipeIssue, validate_recipe
from lsprotocol import types

SOURCE = "esmvaltool"

SEVERITIES = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
}


def recipe_diagnostics(text: str) -> list[types.Diagnostic]:
    """Find the problems in a recipe.

    Parameters
    ----------
    text:
        The YAML text of the recipe.

    Returns
    -------
    :
        The problems in the recipe. YAML syntax errors are reported instead
        of the problems in the recipe content, because the content cannot be
        read.
    """
    lines = text.splitlines()
    try:
        recipe = yaml.safe_load(text)
    except yaml.MarkedYAMLError as exc:
        return [_syntax_diagnostic(exc, lines)]
    except yaml.YAMLError as exc:
        return [_diagnostic(str(exc), 0, 0, lines)]
    return [
        _issue_diagnostic(issue, lines)
        for issue in validate_recipe(recipe, text)
    ]


def _issue_diagnostic(
    issue: RecipeIssue,
    lines: list[str],
) -> types.Diagnostic:
    # RecipeIssue numbers lines and columns from 1, LSP from 0.
    line = (issue.line or 1) - 1
    column = (issue.column or 1) - 1
    message = str(RecipeIssue(issue.path, issue.message))
    return _diagnostic(
        message,
        line,
        column,
        lines,
        SEVERITIES[issue.severity],
    )


def _syntax_diagnostic(
    exc: yaml.MarkedYAMLError,
    lines: list[str],
) -> types.Diagnostic:
    mark = exc.problem_mark or exc.context_mark
    message = " ".join(
        part for part in (exc.context, exc.problem) if part
    ) or str(exc)
    line, column = (mark.line, mark.column) if mark else (0, 0)
    return _diagnostic(f"invalid YAML: {message}", line, column, lines)


def _diagnostic(
    message: str,
    line: int,
    column: int,
    lines: list[str],
    severity: types.DiagnosticSeverity = types.DiagnosticSeverity.Error,
) -> types.Diagnostic:
    """Create a diagnostic from the position to the end of the line."""
    text = lines[line] if line < len(lines) else ""
    end = max(len(text.rstrip()), column + 1)
    return types.Diagnostic(
        range=types.Range(
            start=types.Position(line=line, character=column),
            end=types.Position(line=line, character=end),
        ),
        message=message,
        severity=severity,
        source=SOURCE,
    )
