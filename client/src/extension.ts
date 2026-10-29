import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { PythonExtension } from "@vscode/python-extension";
import * as vscode from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

import { Mode, RECIPE_SELECTOR, SchemaFallback } from "./schema";

const SERVER_MODULE = "esmvaltool_language_server";

let client: LanguageClient | undefined;
let fallback: SchemaFallback | undefined;

// Starting the server takes a few seconds, so restarts can be requested
// while one is still in progress. Starts and stops run one at a time, so
// each start stops the client of the previous one.
let queue: Promise<void> = Promise.resolve();

function enqueue(operation: () => Promise<void>): Promise<void> {
  queue = queue.then(operation, operation);
  return queue;
}

/** The API of this extension, used by its tests. */
export interface Api {
  readonly mode: Mode;
}

export async function activate(context: vscode.ExtensionContext): Promise<Api> {
  const schemaFallback = new SchemaFallback(context.globalStorageUri);
  fallback = schemaFallback;
  context.subscriptions.push(
    schemaFallback,
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
  await enqueue(start);
  return {
    get mode() {
      return schemaFallback.mode;
    },
  };
}

export async function deactivate(): Promise<void> {
  await enqueue(stop);
}

async function start(): Promise<void> {
  const python = await findPython();
  if (!(await hasServer(python))) {
    // Do not wait for the message to be closed, so it does not hold up
    // later restarts.
    void vscode.window
      .showErrorMessage(
        `The ESMValTool language server is not installed in the Python ` +
          `environment of ${python}. Install it in that environment with ` +
          `"pip install esmvaltool-language-server", or select the Python ` +
          `interpreter of an environment where it is installed.`,
        "Select Python Interpreter",
      )
      .then((choice) => {
        if (choice) {
          void vscode.commands.executeCommand("python.setInterpreter");
        }
      });
    await fallback?.useSchema(
      python,
      `The language server is not installed in the environment of ${python}.`,
    );
    return;
  }
  const serverOptions: ServerOptions = {
    command: python,
    args: ["-m", SERVER_MODULE],
  };
  const clientOptions: LanguageClientOptions = {
    documentSelector: [RECIPE_SELECTOR],
  };
  const starting = new LanguageClient(
    "esmvaltool",
    "ESMValTool",
    serverOptions,
    clientOptions,
  );
  try {
    await starting.start();
  } catch (error) {
    await starting.dispose().catch(() => undefined);
    await fallback?.useSchema(
      python,
      `The language server failed to start: ${String(error)}.`,
    );
    return;
  }
  client = starting;
  fallback?.useServer(python);
}

async function stop(): Promise<void> {
  const running = client;
  client = undefined;
  await running?.stop();
}

function restart(): Promise<void> {
  return enqueue(async () => {
    await stop();
    await start();
  });
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
