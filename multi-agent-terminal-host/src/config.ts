import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { z } from "zod";

const runtimeConfigurationSchema = z
  .object({
    allowed_path_roots: z.array(z.string()),
  })
  .passthrough();

export interface TerminalHostConfig {
  readonly host: string;
  readonly port: number;
  readonly statePath: string;
  readonly authToken: string;
  readonly allowedOrigins: ReadonlySet<string>;
  readonly allowedRoots: readonly string[];
  readonly leaseTtlMs: number;
  readonly maxSnapshotScrollback: number;
}

interface ParsedArguments {
  readonly host: string;
  readonly port: number;
  readonly statePath: string;
  readonly authTokenFile: string;
  readonly configurationPath: string;
  readonly allowedOrigins: readonly string[];
}

function takeValue(arguments_: readonly string[], index: number, flag: string): string {
  const value = arguments_[index + 1];
  if (value === undefined || value.startsWith("--")) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

function parseArguments(arguments_: readonly string[]): ParsedArguments {
  let host = "127.0.0.1";
  let port = 8022;
  let statePath = "";
  let authTokenFile = "";
  let configurationPath = "";
  const allowedOrigins: string[] = [];

  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (argument === "--host") {
      host = takeValue(arguments_, index, argument);
      index += 1;
    } else if (argument === "--port") {
      port = Number.parseInt(takeValue(arguments_, index, argument), 10);
      index += 1;
    } else if (argument === "--state-path") {
      statePath = takeValue(arguments_, index, argument);
      index += 1;
    } else if (argument === "--auth-token-file") {
      authTokenFile = takeValue(arguments_, index, argument);
      index += 1;
    } else if (argument === "--configuration-path") {
      configurationPath = takeValue(arguments_, index, argument);
      index += 1;
    } else if (argument === "--allowed-origin") {
      allowedOrigins.push(takeValue(arguments_, index, argument));
      index += 1;
    } else {
      throw new Error(`unknown argument: ${argument}`);
    }
  }

  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("port must be between 1 and 65535");
  }
  if (host !== "127.0.0.1" && host !== "::1" && host !== "localhost") {
    throw new Error("Terminal Host may only bind to a loopback address");
  }
  if (!statePath || !authTokenFile || !configurationPath) {
    throw new Error(
      "--state-path, --auth-token-file, and --configuration-path are required",
    );
  }
  if (allowedOrigins.length === 0) {
    throw new Error("at least one --allowed-origin is required");
  }
  return {
    host,
    port,
    statePath: path.resolve(statePath),
    authTokenFile: path.resolve(authTokenFile),
    configurationPath: path.resolve(configurationPath),
    allowedOrigins,
  };
}

function readAllowedRoots(configurationPath: string): readonly string[] {
  const payload: unknown = JSON.parse(fs.readFileSync(configurationPath, "utf8"));
  const parsed = runtimeConfigurationSchema.parse(payload);
  if (parsed.allowed_path_roots.length === 0) {
    throw new Error("Terminal Host requires at least one configured allowed path root");
  }
  return parsed.allowed_path_roots.map((root) => path.resolve(root));
}

function loadOrCreateAuthToken(tokenPath: string): string {
  fs.mkdirSync(path.dirname(tokenPath), { recursive: true });
  try {
    const existing = fs.readFileSync(tokenPath, "utf8").trim();
    if (existing.length < 32) {
      throw new Error("Terminal Host auth token is invalid");
    }
    return existing;
  } catch (error) {
    if (!(error instanceof Error) || !("code" in error) || error.code !== "ENOENT") {
      throw error;
    }
  }
  const token = crypto.randomBytes(32).toString("base64url");
  fs.writeFileSync(tokenPath, `${token}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  return token;
}

export function loadConfig(arguments_: readonly string[]): TerminalHostConfig {
  const parsed = parseArguments(arguments_);
  return {
    host: parsed.host,
    port: parsed.port,
    statePath: parsed.statePath,
    authToken: loadOrCreateAuthToken(parsed.authTokenFile),
    allowedOrigins: new Set(parsed.allowedOrigins),
    allowedRoots: readAllowedRoots(parsed.configurationPath),
    leaseTtlMs: 30_000,
    maxSnapshotScrollback: 5_000,
  };
}
