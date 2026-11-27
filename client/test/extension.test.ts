// Tests that run the extension and the language server in VSCode.
import * as assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import process from "node:process";

import * as vscode from "vscode";

const TIMEOUT = 50000;

async function waitFor<T>(
  get: () => T | undefined,
  description: string,
): Promise<T> {
  const deadline = Date.now() + TIMEOUT;
  for (;;) {
    const value = get();
    if (value !== undefined) {
      return value;
    }
    if (Date.now() > deadline) {
      throw new Error(`Timed out waiting for ${description}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

/**
 * Create a recipe and a configuration of ESMValCore with local data, so that
 * the datasets are found without searching ESGF.
 */
async function localData(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "esmvaltool-"));
  for (const dataset of ["ERA5", "MERRA2"]) {
    const data = path.join(dir, "data", "Tier3", dataset);
    await fs.mkdir(data, { recursive: true });
    const file = `OBS6_${dataset}_reanaly_v1_Amon_tas_199001-199112.nc`;
    await fs.writeFile(path.join(data, file), "");
  }
  await fs.mkdir(path.join(dir, "config"));
  await fs.writeFile(
    path.join(dir, "config", "data.yml"),
    [
      "projects:",
      "  OBS6:",
      "    data:",
      "      local:",
      "        type: esmvalcore.io.local.LocalDataSource",
      `        rootpath: ${JSON.stringify(path.join(dir, "data"))}`,
      "        dirname_template: Tier{tier}/{dataset}",
      "        filename_template: " +
        "'{project}_{dataset}_{type}_{version}_{mip}_{short_name}[_.]*nc'",
      "",
    ].join("\n"),
  );
  await fs.writeFile(
    path.join(dir, "recipe_populate.yml"),
    [
      "datasets:",
      "  - {dataset: '*', project: OBS6, type: reanaly, version: v1, tier: 3}",
      "diagnostics:",
      "  diag:",
      "    variables:",
      "      tas: {mip: Amon}",
      "    scripts: null",
      "",
    ].join("\n"),
  );
  return dir;
}

/** Get the code lenses of a document, once the language server sends them. */
async function codeLenses(uri: vscode.Uri): Promise<vscode.CodeLens[]> {
  const deadline = Date.now() + TIMEOUT;
  for (;;) {
    const lenses = await vscode.commands.executeCommand<vscode.CodeLens[]>(
      "vscode.executeCodeLensProvider",
      uri,
    );
    if (lenses.length > 0 || Date.now() > deadline) {
      return lenses;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

/** Find the position of `text` in a document, plus `offset` characters. */
function positionOf(
  document: vscode.TextDocument,
  text: string,
  offset = 0,
): vscode.Position {
  const index = document.getText().indexOf(text);
  assert.notEqual(index, -1, `${text} not found`);
  return document.positionAt(index + offset);
}

/** Get the hovers at a position, as Markdown text. */
async function hovers(
  document: vscode.TextDocument,
  position: vscode.Position,
): Promise<string[]> {
  const results = await vscode.commands.executeCommand<vscode.Hover[]>(
    "vscode.executeHoverProvider",
    document.uri,
    position,
  );
  return results.map((hover) =>
    hover.contents
      .map((content) =>
        content instanceof vscode.MarkdownString ? content.value : "",
      )
      .join("\n"),
  );
}

describe("ESMValTool extension", () => {
  let document: vscode.TextDocument;

  before(async () => {
    const folder = vscode.workspace.workspaceFolders?.[0];
    assert.ok(folder);
    document = await vscode.workspace.openTextDocument(
      vscode.Uri.joinPath(folder.uri, "recipe_test.yml"),
    );
    await vscode.window.showTextDocument(document);
  });

  it("reports problems", async () => {
    const diagnostics = await waitFor(() => {
      const found = vscode.languages
        .getDiagnostics(document.uri)
        .filter((diagnostic) => diagnostic.source === "esmvaltool");
      return found.length > 0 ? found : undefined;
    }, "problems");
    assert.equal(diagnostics.length, 1);
    assert.match(diagnostics[0].message, /unknown value 'Amonn'/);
    assert.equal(
      diagnostics[0].range.start.line,
      positionOf(document, "mip: Amonn").line,
    );
  });

  it("suggests values", async () => {
    const list = await vscode.commands.executeCommand<vscode.CompletionList>(
      "vscode.executeCompletionItemProvider",
      document.uri,
      positionOf(document, "mip: Am", "mip: Am".length),
    );
    const labels = list.items.map((item) =>
      typeof item.label === "string" ? item.label : item.label.label,
    );
    assert.equal(labels.filter((label) => label === "Amon").length, 1);
  });

  it("inserts the required arguments of a preprocessor step", async () => {
    const position = positionOf(document, "  pp:\n", "  pp:\n".length);
    const edit = new vscode.WorkspaceEdit();
    edit.insert(document.uri, position, "    area\n");
    assert.ok(await vscode.workspace.applyEdit(edit));
    try {
      const list = await vscode.commands.executeCommand<vscode.CompletionList>(
        "vscode.executeCompletionItemProvider",
        document.uri,
        position.translate(0, "    area".length),
      );
      const item = list.items.find(
        (candidate) => candidate.label === "area_statistics",
      );
      assert.ok(item);
      assert.ok(item.insertText instanceof vscode.SnippetString);
      assert.equal(
        item.insertText.value,
        "area_statistics:\n      operator: ${1}",
      );
      assert.equal(item.keepWhitespace, true);
    } finally {
      const undo = new vscode.WorkspaceEdit();
      undo.delete(
        document.uri,
        new vscode.Range(position, position.translate(1, 0)),
      );
      await vscode.workspace.applyEdit(undo);
    }
  });

  it("shows documentation", async () => {
    const texts = await hovers(document, positionOf(document, "regrid:", 1));
    assert.equal(texts.length, 1);
    assert.match(texts[0], /regridding/);
  });

  it("goes to the definition of a preprocessor", async () => {
    const locations = await vscode.commands.executeCommand<
      (vscode.Location | vscode.LocationLink)[]
    >(
      "vscode.executeDefinitionProvider",
      document.uri,
      positionOf(document, "preprocessor: pp", "preprocessor: p".length),
    );
    assert.equal(locations.length, 1);
    const location = locations[0];
    const range =
      location instanceof vscode.Location
        ? location.range
        : location.targetRange;
    assert.equal(range.start.line, positionOf(document, "  pp:").line);
  });

  it("offers to replace glob patterns by datasets", async () => {
    const position = positionOf(document, "diagnostics:");
    const edit = new vscode.WorkspaceEdit();
    edit.insert(document.uri, position, "datasets: [{dataset: '*'}]\n");
    assert.ok(await vscode.workspace.applyEdit(edit));
    try {
      const lenses = await vscode.commands.executeCommand<vscode.CodeLens[]>(
        "vscode.executeCodeLensProvider",
        document.uri,
      );
      assert.deepEqual(
        lenses.map((lens) => [lens.range.start.line, lens.command?.command]),
        [[position.line, "esmvaltool.populateDatasets"]],
      );
      const actions = await vscode.commands.executeCommand<vscode.CodeAction[]>(
        "vscode.executeCodeActionProvider",
        document.uri,
        new vscode.Range(position, position),
      );
      assert.deepEqual(
        actions.map((action) => [action.kind?.value, action.command?.command]),
        [["refactor.rewrite", "esmvaltool.populateDatasets"]],
      );
    } finally {
      const undo = new vscode.WorkspaceEdit();
      undo.delete(
        document.uri,
        new vscode.Range(position, position.translate(1, 0)),
      );
      await vscode.workspace.applyEdit(undo);
    }
  });

  it("replaces glob patterns by datasets", async () => {
    const dir = await localData();
    const previous = process.env.ESMVALTOOL_CONFIG_DIR;
    process.env.ESMVALTOOL_CONFIG_DIR = path.join(dir, "config");
    try {
      // The language server reads the configuration when it starts.
      await vscode.commands.executeCommand("esmvaltool.restartLanguageServer");
      const recipe = await vscode.workspace.openTextDocument(
        path.join(dir, "recipe_populate.yml"),
      );
      await vscode.window.showTextDocument(recipe);
      const [lens] = await codeLenses(recipe.uri);
      const command = lens.command;
      assert.ok(command);
      const args: unknown[] = command.arguments ?? [];
      await vscode.commands.executeCommand(command.command, ...args);
      assert.deepEqual(recipe.getText().split("\n").slice(1, 3), [
        "  - {dataset: ERA5, project: OBS6, type: reanaly, version: v1, tier: 3}",
        "  - {dataset: MERRA2, project: OBS6, type: reanaly, version: v1, tier: 3}",
      ]);
      await vscode.commands.executeCommand(
        "workbench.action.revertAndCloseActiveEditor",
      );
      await vscode.window.showTextDocument(document);
    } finally {
      if (previous === undefined) {
        delete process.env.ESMVALTOOL_CONFIG_DIR;
      } else {
        process.env.ESMVALTOOL_CONFIG_DIR = previous;
      }
      await vscode.commands.executeCommand("esmvaltool.restartLanguageServer");
      await fs.rm(dir, { recursive: true });
    }
  });

  it("runs a single language server after overlapping restarts", async () => {
    await Promise.all([
      vscode.commands.executeCommand("esmvaltool.restartLanguageServer"),
      vscode.commands.executeCommand("esmvaltool.restartLanguageServer"),
    ]);
    const texts = await hovers(document, positionOf(document, "regrid:", 1));
    assert.equal(texts.length, 1);
  });

  it("accepts output from a language server that is stopping", async () => {
    const extension = vscode.extensions.getExtension<{
      outputChannel: vscode.LogOutputChannel | undefined;
    }>("ESMValGroup.esmvaltool");
    assert.ok(extension);
    const channel = extension.exports.outputChannel;
    assert.ok(channel);
    await vscode.commands.executeCommand("esmvaltool.restartLanguageServer");
    assert.doesNotThrow(() => {
      channel.error("A late message from the previous language server");
    });
  });

  it("logs the messages of the language server at their level", () => {
    const extension = vscode.extensions.getExtension<{
      parseLogLine: (line: string, fallback?: string) => [string, string];
    }>("ESMValGroup.esmvaltool");
    assert.ok(extension);
    const { parseLogLine } = extension.exports;
    assert.deepEqual(parseLogLine("DEBUG:esgf:Read 1 of 1 records"), [
      "debug",
      "esgf:Read 1 of 1 records",
    ]);
    assert.deepEqual(parseLogLine("INFO:server:Started"), [
      "info",
      "server:Started",
    ]);
    assert.deepEqual(parseLogLine("WARNING:esgf:Not asking"), [
      "warn",
      "esgf:Not asking",
    ]);
    assert.deepEqual(parseLogLine("CRITICAL:server:Failed"), [
      "error",
      "server:Failed",
    ]);
    // Lines that were not logged by Python.
    assert.deepEqual(parseLogLine("ERROR 1: PROJ: failed"), [
      "error",
      "ERROR 1: PROJ: failed",
    ]);
    assert.deepEqual(parseLogLine("output", "info"), ["info", "output"]);
  });

  it("does not use the schema for other files named like recipes", async () => {
    const extension = vscode.extensions.getExtension<{
      isRecipe(resource: string): boolean;
    }>("ESMValGroup.esmvaltool");
    assert.ok(extension);
    const folder = vscode.workspace.workspaceFolders?.[0];
    assert.ok(folder);
    const other = await vscode.workspace.openTextDocument(
      vscode.Uri.joinPath(folder.uri, "conda", "recipe.yaml"),
    );
    assert.equal(extension.exports.isRecipe(other.uri.toString()), false);
    assert.equal(extension.exports.isRecipe(document.uri.toString()), true);
  });

  it("sends the errata setting to the language server", async () => {
    const extension = vscode.extensions.getExtension<{
      errataOption: boolean | undefined;
      mode: string;
    }>("ESMValGroup.esmvaltool");
    assert.ok(extension);
    const api = extension.exports;
    assert.equal(api.errataOption, true);
    const settings = vscode.workspace.getConfiguration(
      "esmvaltool.languageServer",
    );
    await settings.update("errata", false, vscode.ConfigurationTarget.Global);
    try {
      await waitFor(
        () => (api.errataOption === false ? true : undefined),
        "the errata setting",
      );
    } finally {
      await settings.update(
        "errata",
        undefined,
        vscode.ConfigurationTarget.Global,
      );
    }
    await waitFor(
      () => (api.errataOption === true ? true : undefined),
      "the default errata setting",
    );
  });

  it("sends the log level of the output to the language server", async () => {
    const extension = vscode.extensions.getExtension<{
      logLevel: string | undefined;
    }>("ESMValGroup.esmvaltool");
    assert.ok(extension);
    const api = extension.exports;
    assert.equal(api.logLevel, "info");
    // Only extension tests can set the log level, of all output channels.
    await vscode.commands.executeCommand(
      "_extensionTests.setLogLevel",
      "debug",
    );
    try {
      await waitFor(
        () => (api.logLevel === "debug" ? true : undefined),
        "the debug log level",
      );
    } finally {
      await vscode.commands.executeCommand(
        "_extensionTests.setLogLevel",
        "info",
      );
    }
    await waitFor(
      () => (api.logLevel === "info" ? true : undefined),
      "the info log level",
    );
  });

  it("falls back to the schema without the language server", async () => {
    const extension = vscode.extensions.getExtension<{ mode: string }>(
      "ESMValGroup.esmvaltool",
    );
    assert.ok(extension);
    const api = extension.exports;
    const settings = vscode.workspace.getConfiguration(
      "esmvaltool.languageServer",
    );
    // Other extensions, including the YAML extension, are disabled in the
    // tests, so recipes cannot be checked against the schema either.
    await settings.update(
      "pythonPath",
      "/does/not/exist/python",
      vscode.ConfigurationTarget.Global,
    );
    try {
      await waitFor(
        () => (api.mode === "none" ? true : undefined),
        "the fallback",
      );
    } finally {
      await settings.update(
        "pythonPath",
        undefined,
        vscode.ConfigurationTarget.Global,
      );
    }
    await waitFor(
      () => (api.mode === "server" ? true : undefined),
      "the language server",
    );
  });
});
