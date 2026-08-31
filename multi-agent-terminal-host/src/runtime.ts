import fs from "node:fs";
import path from "node:path";

import * as pty from "node-pty";

import type { CreateTerminalSession, RuntimeKind } from "./contracts.ts";
import { TerminalHostError } from "./errors.ts";

export interface TerminalProcessExit {
  readonly exitCode: number;
  readonly signal?: number;
}

export interface Disposable {
  dispose(): void;
}

export interface TerminalProcess {
  readonly pid: number;
  onData(listener: (data: string) => void): Disposable;
  onExit(listener: (event: TerminalProcessExit) => void): Disposable;
  write(data: string): void;
  resize(columns: number, rows: number): void;
  kill(signal?: string): void;
}

export interface SpawnSpecification {
  readonly file: string;
  readonly arguments: readonly string[];
  readonly cwd: string;
  readonly environment: Readonly<Record<string, string>>;
  readonly name?: string;
}

export interface TerminalRuntimeAdapter {
  readonly kind: RuntimeKind;
  buildSpawnSpecification(request: CreateTerminalSession): Promise<SpawnSpecification>;
}

export type TerminalProcessFactory = (
  specification: SpawnSpecification,
  columns: number,
  rows: number,
) => TerminalProcess;

function environmentValue(
  environment: Readonly<Record<string, string>>,
  name: string,
): string | undefined {
  const normalizedName = name.toLowerCase();
  return Object.entries(environment).find(
    ([candidate]) => candidate.toLowerCase() === normalizedName,
  )?.[1];
}

function executableNames(
  command: string,
  environment: Readonly<Record<string, string>>,
): readonly string[] {
  if (process.platform !== "win32" || path.extname(command) !== "") {
    return [command];
  }
  const extensions = (environmentValue(environment, "PATHEXT") ?? ".COM;.EXE;.BAT;.CMD")
    .split(";")
    .map((extension) => extension.trim())
    .filter(Boolean);
  return [command, ...extensions.map((extension) => command + extension)];
}

function isExecutableFile(candidate: string): boolean {
  try {
    if (!fs.statSync(candidate).isFile()) {
      return false;
    }
    if (process.platform !== "win32") {
      fs.accessSync(candidate, fs.constants.X_OK);
    }
    return true;
  } catch {
    return false;
  }
}

export function resolveExecutablePath(
  file: string,
  environment: Readonly<Record<string, string>>,
  workingDirectory: string,
): string {
  const command = file.trim();
  if (!command) {
    throw new TerminalHostError(
      "terminal.executable_not_found",
      "terminal executable path is empty",
      422,
    );
  }

  const hasDirectory =
    path.isAbsolute(command) || command.includes("/") || command.includes("\\");
  const directories = hasDirectory
    ? [path.isAbsolute(command) ? "" : workingDirectory]
    : (environmentValue(environment, "PATH") ?? "")
        .split(path.delimiter)
        .map((entry) => entry.trim().replace(/^"|"$/gu, ""))
        .filter(Boolean);
  const names = executableNames(command, environment);

  for (const directory of directories) {
    for (const name of names) {
      const candidate = path.isAbsolute(name) ? name : path.resolve(directory, name);
      if (isExecutableFile(candidate)) {
        return candidate;
      }
    }
  }

  throw new TerminalHostError(
    "terminal.executable_not_found",
    `terminal executable was not found: ${command}`,
    422,
  );
}

export const nodePtyProcessFactory: TerminalProcessFactory = (
  specification,
  columns,
  rows,
) => {
  const executable = resolveExecutablePath(
    specification.file,
    specification.environment,
    specification.cwd,
  );
  try {
    return pty.spawn(executable, [...specification.arguments], {
      name: specification.name ?? "xterm-256color",
      cols: columns,
      rows,
      cwd: specification.cwd,
      env: { ...specification.environment },
      useConpty: true,
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new TerminalHostError(
      "terminal.process_start_failed",
      `failed to start terminal executable ${executable}: ${detail}`,
      500,
    );
  }
};
