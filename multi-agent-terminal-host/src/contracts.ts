import { z } from "zod";

export const runtimeKindSchema = z.enum(["codex", "claude"]);
export type RuntimeKind = z.infer<typeof runtimeKindSchema>;

export const createTerminalSessionSchema = z
  .object({
    delegation_id: z.string().trim().min(1).max(200),
    provider_session_id: z.string().trim().min(1).max(200),
    runtime: runtimeKindSchema,
    cwd: z.string().trim().min(1).max(32_767),
    cols: z.number().int().min(20).max(500).default(120),
    rows: z.number().int().min(5).max(200).default(36),
  })
  .strict();

export type CreateTerminalSession = z.infer<typeof createTerminalSessionSchema>;

export const clientMessageSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("lease.acquire") }).strict(),
  z
    .object({
      type: z.literal("lease.renew"),
      lease_token: z.string().uuid(),
    })
    .strict(),
  z
    .object({
      type: z.literal("lease.release"),
      lease_token: z.string().uuid(),
    })
    .strict(),
  z
    .object({
      type: z.literal("input"),
      lease_token: z.string().uuid(),
      data: z.string().max(65_536),
    })
    .strict(),
  z
    .object({
      type: z.literal("resize"),
      lease_token: z.string().uuid(),
      cols: z.number().int().min(20).max(500),
      rows: z.number().int().min(5).max(200),
    })
    .strict(),
  z.object({ type: z.literal("detach") }).strict(),
]);

export type ClientMessage = z.infer<typeof clientMessageSchema>;

export type TerminalSessionStatus =
  | "starting"
  | "running"
  | "exited"
  | "failed"
  | "terminated";

export interface InputLeaseView {
  readonly client_id: string;
  readonly expires_at: string;
}

export interface TerminalSessionView {
  readonly id: string;
  readonly delegation_id: string;
  readonly provider_session_id: string;
  readonly runtime: RuntimeKind;
  readonly cwd: string;
  readonly cols: number;
  readonly rows: number;
  readonly status: TerminalSessionStatus;
  readonly sequence: number;
  readonly created_at: string;
  readonly updated_at: string;
  readonly exit_code: number | null;
  readonly exit_signal: number | null;
  readonly last_error: string | null;
  readonly input_lease: InputLeaseView | null;
}

export type ServerMessage =
  | {
      readonly type: "snapshot";
      readonly session: TerminalSessionView;
      readonly sequence: number;
      readonly data: string;
    }
  | {
      readonly type: "output";
      readonly sequence: number;
      readonly data: string;
    }
  | {
      readonly type: "session.updated";
      readonly session: TerminalSessionView;
    }
  | {
      readonly type: "lease.granted";
      readonly lease_token: string;
      readonly expires_at: string;
    }
  | {
      readonly type: "lease.released";
    }
  | {
      readonly type: "error";
      readonly code: string;
      readonly message: string;
    };
