import fs from "node:fs";
import path from "node:path";

import { z } from "zod";

import { runtimeKindSchema, type TerminalSessionView } from "./contracts.ts";

const terminalSessionViewSchema = z
  .object({
    id: z.string().uuid(),
    delegation_id: z.string().min(1),
    provider_session_id: z.string().min(1),
    runtime: runtimeKindSchema,
    cwd: z.string().min(1),
    cols: z.number().int(),
    rows: z.number().int(),
    status: z.enum(["starting", "running", "exited", "failed", "terminated"]),
    sequence: z.number().int().nonnegative(),
    created_at: z.string().datetime(),
    updated_at: z.string().datetime(),
    exit_code: z.number().int().nullable(),
    exit_signal: z.number().int().nullable(),
    last_error: z.string().nullable(),
    input_lease: z
      .object({
        client_id: z.string().min(1),
        expires_at: z.string().datetime(),
      })
      .nullable(),
  })
  .strict();

const journalFactSchema = z
  .object({
    type: z.literal("terminal_session.upserted"),
    recorded_at: z.string().datetime(),
    session: terminalSessionViewSchema,
  })
  .strict();

export class SessionJournal {
  readonly #path: string;

  constructor(journalPath: string) {
    this.#path = path.resolve(journalPath);
  }

  open(): Map<string, TerminalSessionView> {
    fs.mkdirSync(path.dirname(this.#path), { recursive: true });
    if (!fs.existsSync(this.#path)) {
      fs.writeFileSync(this.#path, "", { encoding: "utf8", flag: "wx" });
      return new Map();
    }
    const sessions = new Map<string, TerminalSessionView>();
    const content = fs.readFileSync(this.#path, "utf8");
    const lines = content.split(/\r?\n/u);
    for (const [index, line] of lines.entries()) {
      if (!line.trim()) {
        continue;
      }
      let payload: unknown;
      try {
        payload = JSON.parse(line);
      } catch (error) {
        throw new Error(`invalid Terminal Host journal JSON at line ${index + 1}`, {
          cause: error,
        });
      }
      const fact = journalFactSchema.parse(payload);
      sessions.set(fact.session.id, fact.session);
    }
    return sessions;
  }

  append(session: TerminalSessionView): void {
    const fact = {
      type: "terminal_session.upserted",
      recorded_at: new Date().toISOString(),
      session,
    } as const;
    fs.appendFileSync(this.#path, `${JSON.stringify(fact)}\n`, "utf8");
  }
}
