# AGENTS.md

Guidance for AI coding agents working in the ESMValTool language server
repository.
The [README](README.md) describes the features, the installation, and the
development environment; this file summarizes what matters most when making
changes.

This file describes conventions that apply to the whole project. Do not add
notes about individual features or changes to it; document those in the code
or in the README instead. If this file disagrees with the code or
configuration, follow the code and tell the user rather than editing this
file.

## Project overview

This repository contains a
[language server](https://microsoft.github.io/language-server-protocol/) for
[ESMValTool](https://github.com/ESMValGroup/ESMValTool) recipes, built on
[pygls](https://pygls.readthedocs.io), and a Visual Studio Code extension that
starts it.
The server runs in the user's ESMValTool environment and gets its knowledge of
recipes from the installed ESMValCore: the recipe checks
(`esmvalcore.recipe.validate_recipe`), the recipe JSON Schema
(`esmvalcore.recipe.generate_recipe_schema`), the configuration, and the CMOR
tables.
Checks of recipes belong in ESMValCore, so that `esmvaltool run` and the
editor report the same problems; the server only translates them into LSP
messages.

Key locations:

- `src/esmvaltool_language_server/server.py` – the server and the LSP
  handlers, which call the modules below.
- `src/esmvaltool_language_server/diagnostics.py` – problems in a recipe.
- `src/esmvaltool_language_server/completion.py`, `hover.py`,
  `definition.py` – completion, hover, and go to definition.
- `src/esmvaltool_language_server/context.py` – finds where the cursor is
  from the indentation, because a recipe that is being edited is often not
  valid YAML.
- `src/esmvaltool_language_server/schema.py` – looks up the part of the
  recipe schema that applies at a location.
- `src/esmvaltool_language_server/cmor.py` – variables in the CMOR tables.
- `src/esmvaltool_language_server/references.py` – preprocessors, tasks,
  diagnostic scripts, and tags (authors, references, ...) that a recipe refers
  to.
- `client/` – the Visual Studio Code extension (TypeScript), with its tests in
  `client/test/`.
- `tests/` – tests of the server.

## Development environment

The development environment is managed with [pixi](https://pixi.sh) and uses
the development version of ESMValCore (see `pyproject.toml`):

```bash
pixi run test                       # run the tests and measure coverage
pixi run -e py312 test              # run the tests with the oldest Python
pixi run pytest tests/test_context.py   # run a single test file
pixi run test-vscode                # run the extension tests in VSCode
pixi run pre-commit run --all-files # run all linters and formatters
```

The pre-commit hooks for the extension use the tools in
`client/node_modules`, so run `npm install` in `client/` first.
In `client/`, `npm run compile`, `npm run lint`, and `npm run format` check
the types, lint, and format the extension.

## Design

- Keep changes small and focused.
- Keep code and documentation short and simple. Keep functions short (no more
  than about 50 lines).
- Do not add checks of recipes to the server: add them to ESMValCore instead.
  Get what the server needs to know about recipes from ESMValCore, the recipe
  schema, or ESMValTool, rather than hardcoding it, so the server matches the
  user's installation.
- The server must stay responsive while the user types: do slow work in a
  thread or cache it, and handle recipes that are incomplete or not valid
  YAML.
- Keep the editor-specific code in the extension small, so that the features
  work in every editor (the README also describes Emacs).
- Do not copy code from other projects unless its license is compatible with
  Apache 2.0.

## Code style

- Python code is formatted and checked with
  [ruff](https://docs.astral.sh/ruff/) with all rules enabled and a line
  length of 79, and type checked with [pyrefly](https://pyrefly.org); see
  `pyproject.toml`. TypeScript code is formatted with Prettier and checked with
  ESLint and `tsc`. Run pre-commit before finishing a change.
- Write [numpy style docstrings](https://numpydoc.readthedocs.io/en/latest/format.html)
  for public functions, classes, and modules. Private helpers only need a short
  docstring when their purpose is not obvious.
- Add correct type hints to all new Python and TypeScript code.
- Do not add `# noqa`, `# type: ignore`, or `eslint-disable` comments to
  silence a check unless there is no reasonable way to fix the underlying
  issue.
- Match the style, naming, and comment density of the surrounding code.

## Tests

- New and changed code must be covered by tests. Test individual modules in
  `tests/test_<module>.py`, features from the user's point of view in
  `tests/test_features.py`, and the LSP messages in `tests/test_server.py`.
  Mark the cursor in a test recipe with `|` and use the `split_cursor`
  fixture to find its position.
- Changes to the extension, or to what the server sends to the editor, should
  be tested in `client/test/extension.test.ts`.
- Tests use pytest; prefer plain functions, fixtures, and
  `pytest.mark.parametrize` over `unittest` style.
- Do not change existing tests to make new code pass: having to change them
  may mean that existing features break. Flag such changes to the user.

## Documentation

- Update the README when behavior visible to users changes, including the
  settings and commands of the extension.
- Start each sentence on a new line.
- Use inclusive language in documentation, docstrings, and comments. For
  example, write "refer to" instead of "see" and "allowlist" instead of
  "whitelist".

## Backward compatibility

The server must work with the oldest ESMValCore version listed in
`[project] dependencies` and the oldest Python version in `requires-python`
(tested by the `py312` environment). If a change needs a newer ESMValCore,
raise the minimum version and tell the user. The extension must keep working
with the oldest VSCode version in `engines` in `client/package.json`.

## Dependencies

When adding or removing a dependency of the server, update both
`[project] dependencies` (PyPI) and `[tool.pixi.dependencies]` (conda-forge) in
`pyproject.toml`, keep them in sync, and run `pixi lock` to update
`pixi.lock`. Tools that are only needed for development or testing go in
`[tool.pixi.feature.dev.dependencies]`. Dependencies of the extension go in
`client/package.json`; run `npm install` in `client/` to update
`client/package-lock.json`. New dependencies must be actively maintained, and
Python dependencies must be available on conda-forge. Their licenses, and those
of their own dependencies, must be permissive (e.g. MIT, BSD, Apache 2.0) or
weak copyleft (e.g. LGPL, MPL 2.0).

## Pull requests

- Follow the
  [ESMValTool AI policy](https://github.com/ESMValGroup/ESMValTool/blob/main/doc/sphinx/source/community/ai_policy.rst).
  In particular: do not open pull requests autonomously (a human must review
  the code and submit it), do not write pull request descriptions, issue
  descriptions, or comments on behalf of the contributor, and remind the
  contributor to disclose which AI tools were used and for what.
- Use a short, descriptive title that is understandable to users.
