import * as readline from "node:readline";
import type { Readable } from "node:stream";

import type * as vscode from "vscode";

/** The levels of the messages in the output of the language server. */
export type Level = "debug" | "info" | "warn" | "error";

const LEVELS: Record<string, Level> = {
  DEBUG: "debug",
  INFO: "info",
  WARNING: "warn",
  ERROR: "error",
  CRITICAL: "error",
};

// A line logged by Python, e.g. "DEBUG:esmvaltool_language_server.esgf:...".
const LOGGED = /^(DEBUG|INFO|WARNING|ERROR|CRITICAL):(.*)$/s;

/**
 * Get the level and message of a line in the output of the language server.
 *
 * Lines that were not logged by Python, e.g. by libraries written in C, get
 * the fallback level.
 */
export function parseLogLine(
  line: string,
  fallback: Level = "error",
): [Level, string] {
  const match = LOGGED.exec(line);
  return match ? [LEVELS[match[1]], match[2]] : [fallback, line];
}

/** Log the lines of an output of the language server at their level. */
export function logLines(
  fallback: Level,
): (input: Readable, channel: vscode.LogOutputChannel) => void {
  return (input, channel) => {
    readline
      .createInterface({ input, crlfDelay: Infinity, terminal: false })
      .on("line", (line: string) => {
        const [level, message] = parseLogLine(line, fallback);
        channel[level](message);
      });
  };
}
