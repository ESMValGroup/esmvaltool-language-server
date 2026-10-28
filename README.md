# ESMValTool language server

A [language server](https://microsoft.github.io/language-server-protocol/) for
[ESMValTool](https://www.esmvaltool.org) recipes, and a Visual Studio Code
extension that uses it.

The server checks recipes while you write them, with the same checks that
ESMValCore runs before running a recipe.
Problems are shown at the line where they occur.
The server runs in the Python environment where ESMValTool is installed, so
the checks match your installation: the preprocessor functions, the projects
in your configuration, the CMOR tables, and, if ESMValTool is installed, the
authors and references it defines.

## Features

- Problems in recipes, as you type: unknown preprocessor functions and
  arguments, invalid values, missing keys, unknown projects and MIP tables,
  keys that are defined more than once, and YAML syntax errors.

Planned: completion, hover documentation, go to definition, and running a
recipe from the editor.

## Installation

The server requires ESMValCore v2.16 or later.
Install it in the environment where ESMValTool is installed:

```bash
pip install esmvaltool-language-server
```

Then install the ESMValTool extension in Visual Studio Code.
It uses the Python interpreter that is selected in the
[Python extension](https://marketplace.visualstudio.com/items?itemName=ms-python.python),
or the one in the setting `esmvaltool.languageServer.pythonPath`.
It checks files whose name starts with `recipe` and ends with `.yml` or
`.yaml`.

Other editors can start the server with the command
`esmvaltool-language-server`, which communicates over stdin and stdout.

The
[YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)
with the recipe JSON Schema reports many of the same problems.
If you use both, these problems are shown twice.

## Development

The development environment is managed with [pixi](https://pixi.sh) and uses
the development version of ESMValCore in `../esmvalcore`:

```bash
pixi run test                      # run the tests
pixi run ruff check                # check the code
pixi run mypy src tests            # check the type hints
```

To try the Visual Studio Code extension, open the `client` directory in
Visual Studio Code, run `npm install`, and press F5.
In the window that opens, set `esmvaltool.languageServer.pythonPath` to the
Python interpreter of the pixi environment, which is shown by
`pixi run which python`.
To build a package of the extension that can be installed, run
`npx @vscode/vsce package` in the `client` directory.

## License

Apache License 2.0, see [LICENSE](LICENSE).
