import fs from "node:fs";
import path from "node:path";

import { TerminalHostError } from "./errors.ts";

function normalizedPath(value: string): string {
  return path.resolve(value).replaceAll("/", "\\").toLocaleLowerCase("en-US");
}

export class PathPolicy {
  readonly #roots: readonly string[];

  constructor(roots: readonly string[]) {
    this.#roots = roots.map(normalizedPath);
  }

  get roots(): readonly string[] {
    return this.#roots;
  }

  assertAllowed(candidate: string): string {
    const resolved = path.resolve(candidate);
    const normalized = normalizedPath(resolved);
    const allowed = this.#roots.some(
      (root) => normalized === root || normalized.startsWith(`${root}\\`),
    );
    if (!allowed) {
      throw new TerminalHostError(
        "terminal.cwd_not_allowed",
        `working directory is outside the configured allowed roots: ${resolved}`,
        403,
      );
    }
    let stats: fs.Stats;
    try {
      stats = fs.statSync(resolved);
    } catch (error) {
      throw new TerminalHostError(
        "terminal.cwd_unavailable",
        `working directory is unavailable: ${resolved}`,
        400,
      );
    }
    if (!stats.isDirectory()) {
      throw new TerminalHostError(
        "terminal.cwd_not_directory",
        `working directory is not a directory: ${resolved}`,
        400,
      );
    }
    return resolved;
  }
}
