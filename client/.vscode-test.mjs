// Configuration of the tests that run the extension in VSCode.
import process from "node:process";

import { defineConfig } from "@vscode/test-cli";

export default defineConfig({
  tests: [
    {
      files: "out/test/**/*.test.js",
      // Test the oldest version of VSCode in package.json with
      // VSCODE_VERSION=1.93.0.
      version: process.env.VSCODE_VERSION ?? "stable",
      workspaceFolder: "test/fixtures",
      // Do not load other extensions, such as an installed copy of this one.
      launchArgs: ["--disable-extensions"],
      mocha: {
        ui: "bdd",
        // Starting the language server takes a few seconds.
        timeout: 60000,
      },
    },
  ],
  // Used with `vscode-test --coverage`.
  coverage: {
    // Paths relative to the root of the repository, for Codecov. The report
    // includes the dependencies that are bundled into out/extension.js,
    // these are ignored in codecov.yml.
    reporter: { lcovonly: { projectRoot: ".." } },
  },
});
