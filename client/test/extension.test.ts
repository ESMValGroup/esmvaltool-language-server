// Tests that run the extension and the language server in VSCode.
import * as assert from "node:assert/strict";

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

  it("runs a single language server after overlapping restarts", async () => {
    await Promise.all([
      vscode.commands.executeCommand("esmvaltool.restartLanguageServer"),
      vscode.commands.executeCommand("esmvaltool.restartLanguageServer"),
    ]);
    const texts = await hovers(document, positionOf(document, "regrid:", 1));
    assert.equal(texts.length, 1);
  });
});
