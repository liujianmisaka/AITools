import assert from "node:assert/strict";
import test from "node:test";

import { ClaudeTerminalAdapter, CodexTerminalAdapter } from "../src/adapters.ts";
import type { TerminalHostConfig } from "../src/config.ts";
import type { CreateTerminalSession } from "../src/contracts.ts";
import { TerminalHostError } from "../src/errors.ts";

const codexRequest: CreateTerminalSession = {
  delegation_id: "delegation-1",
  provider_id: "codex-local",
  provider_session_id: "thread-1",
  runtime: "codex",
  cwd: "C:\\workspaces\\sample",
  cols: 120,
  rows: 36,
};

const claudeRequest: CreateTerminalSession = {
  ...codexRequest,
  provider_id: "claude-local",
  provider_session_id: "session-1",
  runtime: "claude",
};

function config(
  overrides: Partial<TerminalHostConfig> = {},
): TerminalHostConfig {
  return {
    host: "127.0.0.1",
    port: 8022,
    statePath: "C:\\state\\sessions.jsonl",
    authToken: "x".repeat(32),
    allowedOrigins: new Set(["http://127.0.0.1:5173"]),
    allowedRoots: ["C:\\workspaces"],
    leaseTtlMs: 30_000,
    maxSnapshotScrollback: 5_000,
    providers: [
      {
        providerId: "codex-local",
        kind: "codex",
        codexHome: "C:\\codex-home",
        claudeConfigDir: null,
        claudeCliPath: null,
      },
      {
        providerId: "claude-local",
        kind: "claude",
        codexHome: null,
        claudeConfigDir: "C:\\claude-config",
        claudeCliPath: null,
      },
    ],
    claudeRuntimeMode: "native",
    claudeOpenCodexBaseUrl: "http://127.0.0.1:10100",
    claudeOpenCodexAuthTokenEnv: "ANTHROPIC_AUTH_TOKEN",
    codexRemoteUrl: "ws://127.0.0.1:8048",
    codexBin: "codex",
    claudeLauncher: "claude",
    ...overrides,
  };
}

test("Codex adapter resumes the configured provider session through the App Server", async () => {
  const specification = await new CodexTerminalAdapter(config()).buildSpawnSpecification(
    codexRequest,
  );
  assert.equal(specification.file, "codex");
  assert.deepEqual(specification.arguments, [
    "resume",
    "thread-1",
    "--remote",
    "ws://127.0.0.1:8048",
    "--cd",
    "C:\\workspaces\\sample",
    "--no-alt-screen",
  ]);
  assert.equal(specification.environment.CODEX_HOME, "C:\\codex-home");
});

test("adapters reject an unknown provider and runtime mismatches", async () => {
  await assert.rejects(
    new CodexTerminalAdapter(config()).buildSpawnSpecification({
      ...codexRequest,
      provider_id: "missing",
    }),
    (error: unknown) =>
      error instanceof TerminalHostError && error.code === "terminal.provider_not_found",
  );
  await assert.rejects(
    new CodexTerminalAdapter(config()).buildSpawnSpecification({
      ...codexRequest,
      provider_id: "claude-local",
    }),
    (error: unknown) =>
      error instanceof TerminalHostError &&
      error.code === "terminal.provider_runtime_mismatch",
  );
});

test("Claude native adapter resumes a session and isolates gateway settings", async () => {
  const specification = await new ClaudeTerminalAdapter(config()).buildSpawnSpecification(
    claudeRequest,
  );
  assert.equal(specification.file, "claude");
  assert.deepEqual(specification.arguments, [
    "--resume",
    "session-1",
    "--permission-mode",
    "plan",
  ]);
  assert.equal(specification.environment.CLAUDE_CONFIG_DIR, "C:\\claude-config");
  assert.equal(specification.environment.ANTHROPIC_BASE_URL, undefined);
  assert.equal(specification.environment.CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST, undefined);
});

test("Claude OpenCodex adapter applies the local gateway defaults", async () => {
  const specification = await new ClaudeTerminalAdapter(
    config({ claudeRuntimeMode: "opencodex" }),
  ).buildSpawnSpecification(claudeRequest);
  assert.equal(specification.file, "claude");
  assert.deepEqual(specification.arguments, [
    "--resume",
    "session-1",
    "--permission-mode",
    "plan",
  ]);
  assert.equal(specification.environment.ANTHROPIC_BASE_URL, "http://127.0.0.1:10100");
  assert.equal(specification.environment.ANTHROPIC_AUTH_TOKEN, "opencodex-proxy");
  assert.equal(specification.environment.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY, "1");
});

test("Claude OpenCodex adapter invokes a PowerShell launcher and reads custom auth", async () => {
  const tokenVariable = "AITTOOLS_TEST_CLAUDE_TOKEN";
  const previousToken = process.env[tokenVariable];
  process.env[tokenVariable] = "test-token";
  try {
    const specification = await new ClaudeTerminalAdapter(
      config({
        claudeRuntimeMode: "opencodex",
        claudeOpenCodexBaseUrl: "https://gateway.example.test",
        claudeOpenCodexAuthTokenEnv: tokenVariable,
        claudeLauncher: "C:\\tools\\ocx.ps1",
      }),
    ).buildSpawnSpecification(claudeRequest);
    assert.equal(specification.file, "pwsh.exe");
    assert.deepEqual(specification.arguments, [
      "-NoLogo",
      "-NoProfile",
      "-File",
      "C:\\tools\\ocx.ps1",
      "claude",
      "--resume",
      "session-1",
      "--permission-mode",
      "plan",
    ]);
    assert.equal(specification.environment.ANTHROPIC_AUTH_TOKEN, "test-token");
  } finally {
    if (previousToken === undefined) {
      delete process.env[tokenVariable];
    } else {
      process.env[tokenVariable] = previousToken;
    }
  }
});

test("Claude OpenCodex adapter rejects missing custom auth", async () => {
  const tokenVariable = "AITTOOLS_TEST_MISSING_CLAUDE_TOKEN";
  const previousToken = process.env[tokenVariable];
  delete process.env[tokenVariable];
  try {
    await assert.rejects(
      new ClaudeTerminalAdapter(
        config({
          claudeRuntimeMode: "opencodex",
          claudeOpenCodexBaseUrl: "https://gateway.example.test",
          claudeOpenCodexAuthTokenEnv: tokenVariable,
        }),
      ).buildSpawnSpecification(claudeRequest),
      (error: unknown) =>
        error instanceof TerminalHostError &&
        error.code === "terminal.claude_auth_token_missing",
    );
  } finally {
    if (previousToken !== undefined) {
      process.env[tokenVariable] = previousToken;
    }
  }
});
