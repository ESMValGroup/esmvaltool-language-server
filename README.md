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
- Completion of keys and values: preprocessor functions and their
  arguments, facets, projects, the MIP tables of the project, and branding
  suffixes; the preprocessors and tasks defined in the recipe; and, if
  ESMValTool is installed, its diagnostic scripts, authors, and references.
- Documentation of preprocessor functions and other keys, and of
  preprocessors, when you hover over them.
- Go to the definition of a preprocessor, of a task listed in `ancestors`,
  or of a diagnostic script.

Planned: running a recipe from the editor.

## Installation

The server requires ESMValCore v2.16 or later.
Install it in the environment where ESMValTool is installed:

```bash
pip install esmvaltool-language-server
```

Then set up your editor as described below.
Other editors can start the server with the command
`esmvaltool-language-server --stdio`, which communicates over stdin and
stdout.

### Visual Studio Code

Install the ESMValTool extension.
It uses the Python interpreter that is selected in the
[Python extension](https://marketplace.visualstudio.com/items?itemName=ms-python.python),
or the one in the setting `esmvaltool.languageServer.pythonPath`.
In a workspace with multiple folders, put this setting in the workspace
settings: Visual Studio Code ignores it in the settings of a folder.
The extension checks files whose name starts with `recipe` and ends with
`.yml` or `.yaml`.

The
[YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)
with the recipe JSON Schema reports many of the same problems and offers
many of the same suggestions.
If you use both, these are shown twice, so do not configure the YAML
extension to use the recipe schema.

### Emacs

Use [Eglot](https://www.gnu.org/software/emacs/manual/html_mono/eglot.html),
which is included in Emacs 29 and later.
Add this to your Emacs configuration:

```elisp
(define-derived-mode esmvaltool-recipe-mode yaml-mode "Recipe"
  "Major mode for ESMValTool recipes.")
(add-to-list 'auto-mode-alist
             '("/recipe[^/]*\\.ya?ml\\'" . esmvaltool-recipe-mode))
(with-eval-after-load 'eglot
  (add-to-list 'eglot-server-programs
               '((esmvaltool-recipe-mode :language-id "yaml")
                 . ("esmvaltool-language-server" "--stdio"))))
(add-hook 'esmvaltool-recipe-mode-hook #'eglot-ensure)
```

This defines a mode for recipes, so the server is only used for recipes and
not for other YAML files.
It requires [yaml-mode](https://github.com/yoshiki/yaml-mode); if you use
`yaml-ts-mode` instead, replace `yaml-mode` by `yaml-ts-mode`.
If the environment where the server is installed is not active when Emacs
starts, replace `"esmvaltool-language-server"` by the full path to the
command, for example `"/path/to/env/bin/esmvaltool-language-server"`.
Problems are shown with Flymake, completion works with
`completion-at-point`, documentation is shown by ElDoc, and `M-.` goes to a
definition.

## Development

The development environment is managed with [pixi](https://pixi.sh).
It uses the development version of ESMValCore from GitHub; to use a local
copy instead, see the comment in `pyproject.toml`.

```bash
pixi run test                      # run the tests and measure coverage
pixi run pre-commit install        # check the code before each commit
pixi run pre-commit run -a         # check all code now
```

The pre-commit hooks run ruff and pyrefly from the pixi environment, and
ESLint and Prettier from `client/node_modules`, so run `npm install` in the
`client` directory first.
The Visual Studio Code extension has these commands, to be run in the
`client` directory:

```bash
npm run compile                    # check the types and bundle the extension
npm run lint                       # check the code with ESLint
npm run format                     # format the code with Prettier
npm run package                    # build a package that can be installed
```

To try the extension, open the `client` directory in Visual Studio Code and
press F5.
In the window that opens, set `esmvaltool.languageServer.pythonPath` to the
Python interpreter of the pixi environment, which is shown by
`pixi run which python`.

## License

Apache License 2.0, see [LICENSE](LICENSE).
