import path from "node:path";

import type {
  CreateTerminalSession,
  RuntimeKind,
} from "./contracts.ts";
import type {
  ProviderRuntimeSettings,
  TerminalHostConfig,
} from "./config.ts";
import { TerminalHostError } from "./errors.ts";
import type {
  SpawnSpecification,
  TerminalRuntimeAdapter,
} from "./runtime.ts";

const CLAUDE_GATEWAY_ENVIRONMENT_KEYS = [
  "ANTHROPIC_BASE_URL",
  "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
  "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
  "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
] as const;

function inheritedEnvironment(
  overrides: Readonly<Record<string, string | undefined>> = {},
): Record<string, string> {
  const environment: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined) {
      environment[key] = value;
    }
  }
  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) {
      delete environment[key];
    } else {
      environment[key] = value;
    }
  }
  return environment;
}

function providerFor(
  config: TerminalHostConfig,
  request: CreateTerminalSession,
  expectedKind: RuntimeKind,
): ProviderRuntimeSettings {
  const provider = config.providers.find(
    (candidate) => candidate.providerId === request.provider_id,
  );
  if (provider === undefined) {
    throw new TerminalHostError(
      "terminal.provider_not_found",
      `configured provider was not found: ${request.provider_id}`,
      404,
    );
  }
  if (provider.kind !== expectedKind) {
    throw new TerminalHostError(
      "terminal.provider_runtime_mismatch",
      `provider ${request.provider_id} is configured for ${provider.kind}, not ${expectedKind}`,
      422,
    );
  }
  return provider;
}

function scriptLaunch(
  launcher: string,
  arguments_: readonly string[],
): Pick<SpawnSpecification, "file" | "arguments"> {
  if (path.extname(launcher).toLowerCase() === ".ps1") {
    return {
      file: "pwsh.exe",
      arguments: ["-NoLogo", "-NoProfile", "-File", launcher, ...arguments_],
    };
  }
  return { file: launcher, arguments: arguments_ };
}

function claudeLaunch(
  launcher: string,
  runtimeMode: "native" | "opencodex",
  arguments_: readonly string[],
): Pick<SpawnSpecification, "file" | "arguments"> {
  if (runtimeMode === "native") {
    return scriptLaunch(launcher, arguments_);
  }
  const launcherName = path.basename(launcher).toLowerCase();
  const invokesClaudeDirectly =
    launcherName === "claude" || launcherName === "claude.exe";
  return scriptLaunch(
    launcher,
    invokesClaudeDirectly ? arguments_ : ["claude", ...arguments_],
  );
}

export class CodexTerminalAdapter implements TerminalRuntimeAdapter {
  readonly kind = "codex" as const;

  readonly #config: TerminalHostConfig;

  constructor(config: TerminalHostConfig) {
    this.#config = config;
  }

  async buildSpawnSpecification(
    request: CreateTerminalSession,
  ): Promise<SpawnSpecification> {
    const provider = providerFor(this.#config, request, this.kind);
    if (provider.codexHome === null) {
      throw new TerminalHostError(
        "terminal.codex_home_missing",
        `Codex provider ${provider.providerId} has no configured CODEX_HOME`,
        422,
      );
    }
    return {
      file: this.#config.codexBin,
      arguments: [
        "resume",
        request.provider_session_id,
        "--remote",
        this.#config.codexRemoteUrl,
        "--cd",
        request.cwd,
        "--no-alt-screen",
      ],
      cwd: request.cwd,
      environment: inheritedEnvironment({ CODEX_HOME: provider.codexHome }),
      name: "xterm-256color",
    };
  }
}

export class ClaudeTerminalAdapter implements TerminalRuntimeAdapter {
  readonly kind = "claude" as const;

  readonly #config: TerminalHostConfig;

  constructor(config: TerminalHostConfig) {
    this.#config = config;
  }

  async buildSpawnSpecification(
    request: CreateTerminalSession,
  ): Promise<SpawnSpecification> {
    const provider = providerFor(this.#config, request, this.kind);
    const launcher = provider.claudeCliPath ?? this.#config.claudeLauncher;
    const claudeArguments = [
      "--resume",
      request.provider_session_id,
      "--permission-mode",
      "plan",
    ];
    const launch = claudeLaunch(launcher, this.#config.claudeRuntimeMode, claudeArguments);
    const environment = this.#claudeEnvironment();
    if (provider.claudeConfigDir !== null) {
      environment.CLAUDE_CONFIG_DIR = provider.claudeConfigDir;
    }
    return {
      ...launch,
      cwd: request.cwd,
      environment,
      name: "xterm-256color",
    };
  }

  #claudeEnvironment(): Record<string, string> {
    if (this.#config.claudeRuntimeMode === "native") {
      return inheritedEnvironment(
        Object.fromEntries(
          CLAUDE_GATEWAY_ENVIRONMENT_KEYS.map((key) => [key, undefined]),
        ),
      );
    }

    const configuredToken = process.env[this.#config.claudeOpenCodexAuthTokenEnv]?.trim();
    const authToken =
      configuredToken ||
      (this.#config.claudeOpenCodexBaseUrl === "http://127.0.0.1:10100" &&
      this.#config.claudeOpenCodexAuthTokenEnv === "ANTHROPIC_AUTH_TOKEN"
        ? "opencodex-proxy"
        : undefined);
    if (!authToken) {
      throw new TerminalHostError(
        "terminal.claude_auth_token_missing",
        `Claude OpenCodex mode requires ${this.#config.claudeOpenCodexAuthTokenEnv}`,
        422,
      );
    }
    return inheritedEnvironment({
      ANTHROPIC_BASE_URL: this.#config.claudeOpenCodexBaseUrl,
      ANTHROPIC_AUTH_TOKEN: authToken,
      CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY: "1",
      CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST: "1",
      CLAUDE_CODE_AUTO_COMPACT_WINDOW: "829800",
    });
  }
}
