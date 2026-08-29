import crypto from "node:crypto";

import type {
  CreateTerminalSession,
  RuntimeKind,
  TerminalSessionView,
} from "./contracts.ts";
import { TerminalHostError } from "./errors.ts";
import { PathPolicy } from "./path-policy.ts";
import type {
  TerminalProcessFactory,
  TerminalRuntimeAdapter,
} from "./runtime.ts";
import { SessionJournal } from "./session-journal.ts";
import { TerminalSession } from "./terminal-session.ts";

export interface SessionManagerOptions {
  readonly pathPolicy: PathPolicy;
  readonly journal: SessionJournal;
  readonly processFactory: TerminalProcessFactory;
  readonly adapters: readonly TerminalRuntimeAdapter[];
  readonly leaseTtlMs: number;
  readonly maxSnapshotScrollback: number;
}

export class SessionManager {
  readonly #pathPolicy: PathPolicy;
  readonly #journal: SessionJournal;
  readonly #processFactory: TerminalProcessFactory;
  readonly #adapters: ReadonlyMap<RuntimeKind, TerminalRuntimeAdapter>;
  readonly #leaseTtlMs: number;
  readonly #maxSnapshotScrollback: number;
  readonly #historicalSessions: Map<string, TerminalSessionView>;
  readonly #liveSessions = new Map<string, TerminalSession>();

  constructor(options: SessionManagerOptions) {
    this.#pathPolicy = options.pathPolicy;
    this.#journal = options.journal;
    this.#processFactory = options.processFactory;
    this.#leaseTtlMs = options.leaseTtlMs;
    this.#maxSnapshotScrollback = options.maxSnapshotScrollback;
    this.#historicalSessions = options.journal.open();
    this.#reconcileInterruptedSessions();
    this.#adapters = new Map(options.adapters.map((adapter) => [adapter.kind, adapter]));
    if (this.#adapters.size !== options.adapters.length) {
      throw new Error("terminal runtime adapter kinds must be unique");
    }
  }

  list(delegationId?: string): readonly TerminalSessionView[] {
    const sessions = new Map(this.#historicalSessions);
    for (const [sessionId, session] of this.#liveSessions) {
      sessions.set(sessionId, session.view());
    }
    return [...sessions.values()]
      .filter((session) => delegationId === undefined || session.delegation_id === delegationId)
      .sort((left, right) => right.created_at.localeCompare(left.created_at));
  }

  get(sessionId: string): TerminalSessionView {
    const live = this.#liveSessions.get(sessionId);
    if (live !== undefined) {
      return live.view();
    }
    const historical = this.#historicalSessions.get(sessionId);
    if (historical === undefined) {
      throw new TerminalHostError(
        "terminal.session_not_found",
        `terminal session was not found: ${sessionId}`,
        404,
      );
    }
    return historical;
  }

  getLive(sessionId: string): TerminalSession {
    const session = this.#liveSessions.get(sessionId);
    if (session === undefined) {
      this.get(sessionId);
      throw new TerminalHostError(
        "terminal.session_not_live",
        `terminal session is not attached to this host process: ${sessionId}`,
      );
    }
    return session;
  }

  async create(request: CreateTerminalSession): Promise<TerminalSessionView> {
    const existing = this.list(request.delegation_id).find(
      (session) =>
        session.provider_id === request.provider_id &&
        session.provider_session_id === request.provider_session_id &&
        session.runtime === request.runtime &&
        (session.status === "starting" || session.status === "running"),
    );
    if (existing !== undefined) {
      return existing;
    }
    const adapter = this.#adapters.get(request.runtime);
    if (adapter === undefined) {
      throw new TerminalHostError(
        "terminal.runtime_not_available",
        `terminal runtime is not available: ${request.runtime}`,
        503,
      );
    }
    const cwd = this.#pathPolicy.assertAllowed(request.cwd);
    const normalizedRequest = { ...request, cwd };
    const specification = await adapter.buildSpawnSpecification(normalizedRequest);
    if (specification.cwd !== cwd) {
      throw new Error("terminal runtime adapter changed the validated working directory");
    }
    const process = this.#processFactory(specification, request.cols, request.rows);
    const id = crypto.randomUUID();
    const session = new TerminalSession({
      id,
      delegationId: request.delegation_id,
      providerId: request.provider_id,
      providerSessionId: request.provider_session_id,
      runtime: request.runtime,
      cwd,
      cols: request.cols,
      rows: request.rows,
      leaseTtlMs: this.#leaseTtlMs,
      maxSnapshotScrollback: this.#maxSnapshotScrollback,
      process,
      onDurableChange: (view) => {
        this.#historicalSessions.set(view.id, view);
        this.#journal.append(view);
      },
    });
    this.#liveSessions.set(id, session);
    return session.view();
  }

  terminate(sessionId: string): TerminalSessionView {
    const session = this.getLive(sessionId);
    session.terminate();
    return session.view();
  }

  close(): void {
    for (const session of this.#liveSessions.values()) {
      session.terminate();
      session.dispose();
    }
    this.#liveSessions.clear();
  }

  #reconcileInterruptedSessions(): void {
    for (const [sessionId, session] of this.#historicalSessions) {
      if (session.status !== "starting" && session.status !== "running") {
        continue;
      }
      const reconciled: TerminalSessionView = {
        ...session,
        status: "failed",
        updated_at: new Date().toISOString(),
        last_error: "Terminal Host restarted before the PTY reached a terminal state",
        input_lease: null,
      };
      this.#historicalSessions.set(sessionId, reconciled);
      this.#journal.append(reconciled);
    }
  }
}
