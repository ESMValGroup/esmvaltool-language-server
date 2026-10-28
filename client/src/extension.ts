import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { PythonExtension } from "@vscode/python-extension";
import * as vscode from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

const SERVER_MODULE = "esmvaltool_language_server";

let client: LanguageClient | undefined;

export async function activate(
  context: vscode.ExtensionContext,
): Promise<void> {
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "esmvaltool.restartLanguageServer",
      restart,
    ),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("esmvaltool.languageServer")) {
        void restart();
      }
    }),
  );
  const python = await getPythonExtension();
  if (python) {
    context.subscriptions.push(
      python.environments.onDidChangeActiveEnvironmentPath(() => restart()),
    );
  }
  await start();
}

export async function deactivate(): Promise<void> {
  await stop();
}

async function start(): Promise<void> {
  const python = await findPython();
  if (!(await hasServer(python))) {
    const choice = await vscode.window.showErrorMessage(
      `The ESMValTool language server is not installed in the Python ` +
        `environment of ${python}. Install it in that environment with ` +
        `"pip install esmvaltool-language-server", or select the Python ` +
        `interpreter of an environment where it is installed.`,
      "Select Python Interpreter",
    );
    if (choice) {
      await vscode.commands.executeCommand("python.setInterpreter");
    }
    return;
  }
  const serverOptions: ServerOptions = {
    command: python,
    args: ["-m", SERVER_MODULE],
  };
  const clientOptions: LanguageClientOptions = {
    documentSelector: [
      { scheme: "file", language: "yaml", pattern: "**/recipe*.{yml,yaml}" },
    ],
  };
  client = new LanguageClient(
    "esmvaltool",
    "ESMValTool",
    serverOptions,
    clientOptions,
  );
  await client.start();
}

async function stop(): Promise<void> {
  const running = client;
  client = undefined;
  await running?.stop();
}

async function restart(): Promise<void> {
  await stop();
  await start();
}

/** Find the Python interpreter of the environment with ESMValTool. */
async function findPython(): Promise<string> {
  const configured = vscode.workspace
    .getConfiguration("esmvaltool.languageServer")
    .get<string>("pythonPath");
  if (configured) {
    return configured;
  }
  const python = await getPythonExtension();
  if (python) {
    const path = python.environments.getActiveEnvironmentPath();
    const environment = await python.environments.resolveEnvironment(path);
    if (environment?.executable.uri) {
      return environment.executable.uri.fsPath;
    }
  }
  return "python3";
}

async function getPythonExtension(): Promise<PythonExtension | undefined> {
  try {
    return await PythonExtension.api();
  } catch {
    // The Python extension is not installed.
    return undefined;
  }
}

async function hasServer(python: string): Promise<boolean> {
  try {
    await promisify(execFile)(python, ["-c", `import ${SERVER_MODULE}`]);
    return true;
  } catch {
    return false;
  }
}
