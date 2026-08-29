import * as pty from "node-pty";

import type { CreateTerminalSession, RuntimeKind } from "./contracts.ts";

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

export const nodePtyProcessFactory: TerminalProcessFactory = (
  specification,
  columns,
  rows,
) =>
  pty.spawn(specification.file, [...specification.arguments], {
    name: specification.name ?? "xterm-256color",
    cols: columns,
    rows,
    cwd: specification.cwd,
    env: { ...specification.environment },
    useConpty: true,
  });
