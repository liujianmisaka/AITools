import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { TerminalHostError } from "../src/errors.ts";
import { PathPolicy } from "../src/path-policy.ts";
import type {
  Disposable,
  SpawnSpecification,
  TerminalProcess,
  TerminalProcessExit,
  TerminalRuntimeAdapter,
} from "../src/runtime.ts";
import { SessionJournal } from "../src/session-journal.ts";
import { SessionManager } from "../src/session-manager.ts";

class FakeProcess implements TerminalProcess {
  readonly pid = 42;
  readonly writes: string[] = [];
  readonly sizes: Array<readonly [number, number]> = [];
  readonly #dataListeners = new Set<(data: string) => void>();
  readonly #exitListeners = new Set<(event: TerminalProcessExit) => void>();

  onData(listener: (data: string) => void): Disposable {
    this.#dataListeners.add(listener);
    return { dispose: () => this.#dataListeners.delete(listener) };
  }

  onExit(listener: (event: TerminalProcessExit) => void): Disposable {
    this.#exitListeners.add(listener);
    return { dispose: () => this.#exitListeners.delete(listener) };
  }

  write(data: string): void {
    this.writes.push(data);
  }

  resize(columns: number, rows: number): void {
    this.sizes.push([columns, rows]);
  }

  kill(): void {
    this.exit(0, 0);
  }

  emit(data: string): void {
    for (const listener of this.#dataListeners) {
      listener(data);
    }
  }

  exit(exitCode: number, signal: number): void {
    for (const listener of this.#exitListeners) {
      listener({ exitCode, signal });
    }
  }
}

const fakeAdapter: TerminalRuntimeAdapter = {
  kind: "codex",
  async buildSpawnSpecification(request): Promise<SpawnSpecification> {
    return {
      file: "fake",
      arguments: [],
      cwd: request.cwd,
      environment: {},
    };
  },
};

test("creates a terminal, serializes output, and enforces one input lease", async (context) => {
  const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "terminal-host-test-"));
  context.after(() => fs.rmSync(temporaryDirectory, { recursive: true, force: true }));
  const workspace = path.join(temporaryDirectory, "workspace");
  fs.mkdirSync(workspace);
  const fakeProcess = new FakeProcess();
  const manager = new SessionManager({
    pathPolicy: new PathPolicy([workspace]),
    journal: new SessionJournal(path.join(temporaryDirectory, "sessions.jsonl")),
    processFactory: () => fakeProcess,
    adapters: [fakeAdapter],
    leaseTtlMs: 30_000,
    maxSnapshotScrollback: 100,
  });

  const view = await manager.create({
    delegation_id: "delegation-1",
    provider_session_id: "thread-1",
    runtime: "codex",
    cwd: workspace,
    cols: 80,
    rows: 24,
  });
  const duplicate = await manager.create({
    delegation_id: "delegation-1",
    provider_session_id: "thread-1",
    runtime: "codex",
    cwd: workspace,
    cols: 80,
    rows: 24,
  });
  assert.equal(duplicate.id, view.id);

  const session = manager.getLive(view.id);
  fakeProcess.emit("hello\r\n");
  const snapshot = await session.snapshot();
  assert.match(snapshot.data, /hello/u);
  assert.equal(snapshot.sequence, 1);

  const lease = session.acquireLease("client-1");
  assert.throws(
    () => session.acquireLease("client-2"),
    (error: unknown) =>
      error instanceof TerminalHostError && error.code === "terminal.input_lease_conflict",
  );
  session.write("client-1", lease.leaseToken, "input");
  session.resize("client-1", lease.leaseToken, 100, 40);
  assert.deepEqual(fakeProcess.writes, ["input"]);
  assert.deepEqual(fakeProcess.sizes, [[100, 40]]);
  session.releaseLease("client-1", lease.leaseToken);
  assert.equal(session.view().input_lease, null);
});

test("rejects working directories outside configured roots", async (context) => {
  const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "terminal-host-path-"));
  context.after(() => fs.rmSync(temporaryDirectory, { recursive: true, force: true }));
  const workspace = path.join(temporaryDirectory, "workspace");
  const outside = path.join(temporaryDirectory, "outside");
  fs.mkdirSync(workspace);
  fs.mkdirSync(outside);
  const manager = new SessionManager({
    pathPolicy: new PathPolicy([workspace]),
    journal: new SessionJournal(path.join(temporaryDirectory, "sessions.jsonl")),
    processFactory: () => new FakeProcess(),
    adapters: [fakeAdapter],
    leaseTtlMs: 30_000,
    maxSnapshotScrollback: 100,
  });

  await assert.rejects(
    manager.create({
      delegation_id: "delegation-2",
      provider_session_id: "thread-2",
      runtime: "codex",
      cwd: outside,
      cols: 80,
      rows: 24,
    }),
    (error: unknown) =>
      error instanceof TerminalHostError && error.code === "terminal.cwd_not_allowed",
  );
});

test("reconciles sessions that were live when the host restarted", async (context) => {
  const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "terminal-host-replay-"));
  context.after(() => fs.rmSync(temporaryDirectory, { recursive: true, force: true }));
  const workspace = path.join(temporaryDirectory, "workspace");
  fs.mkdirSync(workspace);
  const journalPath = path.join(temporaryDirectory, "sessions.jsonl");
  const first = new SessionManager({
    pathPolicy: new PathPolicy([workspace]),
    journal: new SessionJournal(journalPath),
    processFactory: () => new FakeProcess(),
    adapters: [fakeAdapter],
    leaseTtlMs: 30_000,
    maxSnapshotScrollback: 100,
  });
  const created = await first.create({
    delegation_id: "delegation-replay",
    provider_session_id: "thread-replay",
    runtime: "codex",
    cwd: workspace,
    cols: 80,
    rows: 24,
  });

  const replayed = new SessionManager({
    pathPolicy: new PathPolicy([workspace]),
    journal: new SessionJournal(journalPath),
    processFactory: () => new FakeProcess(),
    adapters: [fakeAdapter],
    leaseTtlMs: 30_000,
    maxSnapshotScrollback: 100,
  });
  const view = replayed.get(created.id);
  assert.equal(view.status, "failed");
  assert.match(view.last_error ?? "", /restarted/u);
  assert.equal(view.input_lease, null);
});
