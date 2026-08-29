import crypto from "node:crypto";
import { createRequire } from "node:module";

import { SerializeAddon } from "@xterm/addon-serialize";
import type { ITerminalAddon, Terminal as HeadlessTerminal } from "@xterm/headless";

import type {
  InputLeaseView,
  RuntimeKind,
  ServerMessage,
  TerminalSessionStatus,
  TerminalSessionView,
} from "./contracts.ts";
import { TerminalHostError } from "./errors.ts";
import type { Disposable, TerminalProcess } from "./runtime.ts";

const require = createRequire(import.meta.url);
const { Terminal } = require("@xterm/headless") as {
  Terminal: typeof HeadlessTerminal;
};

interface InputLease {
  readonly clientId: string;
  readonly token: string;
  expiresAtMs: number;
}

export interface TerminalSessionOptions {
  readonly id: string;
  readonly delegationId: string;
  readonly providerId: string;
  readonly providerSessionId: string;
  readonly runtime: RuntimeKind;
  readonly cwd: string;
  readonly cols: number;
  readonly rows: number;
  readonly leaseTtlMs: number;
  readonly maxSnapshotScrollback: number;
  readonly process: TerminalProcess;
  readonly onDurableChange: (view: TerminalSessionView) => void;
}

type Subscriber = (message: ServerMessage) => void;

export class TerminalSession {
  readonly #process: TerminalProcess;
  readonly #terminal: HeadlessTerminal;
  readonly #serializeAddon: SerializeAddon;
  readonly #subscribers = new Set<Subscriber>();
  readonly #subscriptions: Disposable[];
  readonly #leaseTtlMs: number;
  readonly #maxSnapshotScrollback: number;
  readonly #onDurableChange: (view: TerminalSessionView) => void;
  #writeTail: Promise<void> = Promise.resolve();
  #status: TerminalSessionStatus = "starting";
  #sequence = 0;
  #updatedAt = new Date();
  #cols: number;
  #rows: number;
  #exitCode: number | null = null;
  #exitSignal: number | null = null;
  #lastError: string | null = null;
  #lease: InputLease | null = null;

  readonly id: string;
  readonly delegationId: string;
  readonly providerId: string;
  readonly providerSessionId: string;
  readonly runtime: RuntimeKind;
  readonly cwd: string;
  readonly createdAt = new Date();

  constructor(options: TerminalSessionOptions) {
    this.id = options.id;
    this.delegationId = options.delegationId;
    this.providerId = options.providerId;
    this.providerSessionId = options.providerSessionId;
    this.runtime = options.runtime;
    this.cwd = options.cwd;
    this.#cols = options.cols;
    this.#rows = options.rows;
    this.#leaseTtlMs = options.leaseTtlMs;
    this.#maxSnapshotScrollback = options.maxSnapshotScrollback;
    this.#process = options.process;
    this.#onDurableChange = options.onDurableChange;
    this.#terminal = new Terminal({
      allowProposedApi: true,
      cols: options.cols,
      rows: options.rows,
      scrollback: options.maxSnapshotScrollback,
    });
    this.#serializeAddon = new SerializeAddon();
    this.#terminal.loadAddon(this.#serializeAddon as unknown as ITerminalAddon);
    this.#subscriptions = [
      this.#process.onData((data) => this.#enqueueOutput(data)),
      this.#process.onExit((event) => {
        this.#exitCode = event.exitCode;
        this.#exitSignal = event.signal ?? null;
        if (this.#status !== "terminated") {
          this.#status = event.exitCode === 0 ? "exited" : "failed";
        }
        this.#touch(true);
      }),
    ];
    this.#status = "running";
    this.#touch(true);
  }

  get status(): TerminalSessionStatus {
    return this.#status;
  }

  view(): TerminalSessionView {
    this.#expireLease();
    const leaseView: InputLeaseView | null =
      this.#lease === null
        ? null
        : {
            client_id: this.#lease.clientId,
            expires_at: new Date(this.#lease.expiresAtMs).toISOString(),
          };
    return {
      id: this.id,
      delegation_id: this.delegationId,
      provider_id: this.providerId,
      provider_session_id: this.providerSessionId,
      runtime: this.runtime,
      cwd: this.cwd,
      cols: this.#cols,
      rows: this.#rows,
      status: this.#status,
      sequence: this.#sequence,
      created_at: this.createdAt.toISOString(),
      updated_at: this.#updatedAt.toISOString(),
      exit_code: this.#exitCode,
      exit_signal: this.#exitSignal,
      last_error: this.#lastError,
      input_lease: leaseView,
    };
  }

  subscribe(subscriber: Subscriber): () => void {
    this.#subscribers.add(subscriber);
    return () => this.#subscribers.delete(subscriber);
  }

  async snapshot(): Promise<Extract<ServerMessage, { type: "snapshot" }>> {
    await this.#writeTail;
    return {
      type: "snapshot",
      session: this.view(),
      sequence: this.#sequence,
      data: this.#serializeAddon.serialize({
        scrollback: this.#maxSnapshotScrollback,
      }),
    };
  }

  acquireLease(clientId: string): { leaseToken: string; expiresAt: string } {
    this.#requireRunning();
    this.#expireLease();
    if (this.#lease !== null && this.#lease.clientId !== clientId) {
      throw new TerminalHostError(
        "terminal.input_lease_conflict",
        "another client currently controls terminal input",
      );
    }
    const expiresAtMs = Date.now() + this.#leaseTtlMs;
    const token = this.#lease?.token ?? crypto.randomUUID();
    this.#lease = { clientId, token, expiresAtMs };
    this.#touch(true);
    return { leaseToken: token, expiresAt: new Date(expiresAtMs).toISOString() };
  }

  renewLease(clientId: string, token: string): string {
    const lease = this.#requireLease(clientId, token);
    lease.expiresAtMs = Date.now() + this.#leaseTtlMs;
    this.#touch(true);
    return new Date(lease.expiresAtMs).toISOString();
  }

  releaseLease(clientId: string, token: string): void {
    this.#requireLease(clientId, token);
    this.#lease = null;
    this.#touch(true);
  }

  write(clientId: string, token: string, data: string): void {
    this.#requireRunning();
    this.#requireLease(clientId, token);
    this.#process.write(data);
  }

  resize(clientId: string, token: string, columns: number, rows: number): void {
    this.#requireRunning();
    this.#requireLease(clientId, token);
    this.#process.resize(columns, rows);
    this.#terminal.resize(columns, rows);
    this.#cols = columns;
    this.#rows = rows;
    this.#touch(true);
  }

  terminate(): void {
    if (this.#status !== "starting" && this.#status !== "running") {
      return;
    }
    this.#status = "terminated";
    this.#lease = null;
    this.#touch(true);
    this.#process.kill();
  }

  dispose(): void {
    for (const subscription of this.#subscriptions) {
      subscription.dispose();
    }
    this.#terminal.dispose();
    this.#subscribers.clear();
  }

  #enqueueOutput(data: string): void {
    this.#writeTail = this.#writeTail
      .then(
        () =>
          new Promise<void>((resolve) => {
            this.#terminal.write(data, resolve);
          }),
      )
      .then(() => {
        this.#sequence += 1;
        this.#updatedAt = new Date();
        this.#broadcast({ type: "output", sequence: this.#sequence, data });
      })
      .catch((error: unknown) => {
        this.#lastError = error instanceof Error ? error.message : String(error);
        this.#status = "failed";
        this.#touch(true);
      });
  }

  #touch(durable: boolean): void {
    this.#updatedAt = new Date();
    const view = this.view();
    if (durable) {
      this.#onDurableChange(view);
    }
    this.#broadcast({ type: "session.updated", session: view });
  }

  #broadcast(message: ServerMessage): void {
    for (const subscriber of this.#subscribers) {
      subscriber(message);
    }
  }

  #expireLease(): void {
    if (this.#lease !== null && this.#lease.expiresAtMs <= Date.now()) {
      this.#lease = null;
    }
  }

  #requireLease(clientId: string, token: string): InputLease {
    this.#expireLease();
    if (
      this.#lease === null ||
      this.#lease.clientId !== clientId ||
      this.#lease.token !== token
    ) {
      throw new TerminalHostError(
        "terminal.input_lease_required",
        "a current input lease is required",
        403,
      );
    }
    return this.#lease;
  }

  #requireRunning(): void {
    if (this.#status !== "running") {
      throw new TerminalHostError(
        "terminal.session_not_running",
        `terminal session is ${this.#status}`,
      );
    }
  }
}
