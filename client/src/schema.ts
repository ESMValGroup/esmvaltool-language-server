import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import * as vscode from "vscode";

export const RECIPE_SELECTOR = {
  scheme: "file",
  language: "yaml",
  pattern: "**/recipe*.{yml,yaml}",
} satisfies vscode.DocumentFilter;

const YAML_EXTENSION = "redhat.vscode-yaml";

const PUBLISHED_SCHEMA =
  "https://docs.esmvaltool.org/projects/ESMValCore/en/latest/recipe_schema.json";

const WRITE_SCHEMA =
  "import sys; from esmvalcore.recipe import write_recipe_schema; " +
  "write_recipe_schema(sys.argv[1])";

/** How recipes are checked. */
export type Mode = "starting" | "server" | "schema" | "none";

/** The part of the API of the YAML extension that is used here. */
interface YamlExtensionApi {
  registerContributor(
    scheme: string,
    requestSchema: (resource: string) => string | undefined,
    requestSchemaContent: (uri: string) => string,
  ): boolean;
}

/**
 * Check recipes with the recipe schema in the YAML extension while the
 * language server is not running.
 *
 * The language server reports the same problems as the schema, so the
 * schema is only used when the server cannot run.
 */
export class SchemaFallback implements vscode.Disposable {
  mode: Mode = "starting";
  private schema: string | undefined;
  private yaml: YamlExtensionApi | undefined;
  private readonly status: vscode.LanguageStatusItem;

  constructor(private readonly storage: vscode.Uri) {
    this.status = vscode.languages.createLanguageStatusItem(
      "esmvaltool",
      RECIPE_SELECTOR,
    );
    this.status.name = "ESMValTool";
    this.status.text = "Starting language server";
    this.status.busy = true;
  }

  dispose(): void {
    this.status.dispose();
  }

  /** Use the language server, not the schema. */
  useServer(python: string): void {
    this.schema = undefined;
    this.setStatus("server", "Language server", python);
  }

  /** Use the schema, because the language server cannot run. */
  async useSchema(python: string, reason: string): Promise<void> {
    const yaml = await this.getYamlExtension();
    if (!yaml) {
      this.schema = undefined;
      this.setStatus(
        "none",
        "Not checked",
        `${reason} Install the YAML extension to check recipes against ` +
          `the recipe schema instead.`,
        {
          title: "Install YAML Extension",
          command: "workbench.extensions.search",
          arguments: [YAML_EXTENSION],
        },
      );
      return;
    }
    this.schema = await this.writeSchema(python);
    this.setStatus(
      "schema",
      "Schema only",
      `${reason} Recipes are checked against the recipe schema by the ` +
        `YAML extension.`,
      { title: "Select Python Interpreter", command: "python.setInterpreter" },
    );
  }

  private setStatus(
    mode: Mode,
    text: string,
    detail: string,
    command?: vscode.Command,
  ): void {
    this.mode = mode;
    this.status.busy = false;
    this.status.text = text;
    this.status.detail = detail;
    this.status.command = command;
    this.status.severity =
      mode === "server"
        ? vscode.LanguageStatusSeverity.Information
        : vscode.LanguageStatusSeverity.Warning;
  }

  private async getYamlExtension(): Promise<YamlExtensionApi | undefined> {
    if (!this.yaml) {
      const extension = vscode.extensions.getExtension(YAML_EXTENSION);
      if (!extension) {
        return undefined;
      }
      const yaml = (await extension.activate()) as YamlExtensionApi;
      // The YAML extension asks for the schema of each file it checks.
      // Schemas are returned as file or https URIs, which the YAML
      // extension loads itself, so the content is never requested.
      yaml.registerContributor(
        "esmvaltool",
        (resource) => (isRecipe(resource) ? this.schema : undefined),
        () => "",
      );
      this.yaml = yaml;
    }
    return this.yaml;
  }

  /**
   * Write the schema of the ESMValCore installed with `python`.
   *
   * Returns the URI of the schema, or of the schema of the latest
   * ESMValCore release if that ESMValCore is missing or too old.
   */
  private async writeSchema(python: string): Promise<string> {
    // Use one file per interpreter, because the YAML extension caches
    // schemas by URI.
    const name = createHash("sha256").update(python).digest("hex");
    const file = vscode.Uri.joinPath(
      this.storage,
      `recipe_schema-${name.slice(0, 16)}.json`,
    );
    try {
      await vscode.workspace.fs.createDirectory(this.storage);
      await promisify(execFile)(python, ["-c", WRITE_SCHEMA, file.fsPath]);
      // The storage directory may have a scheme such as vscode-userdata,
      // which the YAML extension cannot load.
      return vscode.Uri.file(file.fsPath).toString();
    } catch {
      return PUBLISHED_SCHEMA;
    }
  }
}

function isRecipe(resource: string): boolean {
  const path = vscode.Uri.parse(resource).path;
  return /\/recipe[^/]*\.ya?ml$/.test(path);
}
