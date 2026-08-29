import { loadConfig } from "./config.ts";
import { PathPolicy } from "./path-policy.ts";
import { nodePtyProcessFactory } from "./runtime.ts";
import { TerminalHostServer } from "./server.ts";
import { SessionJournal } from "./session-journal.ts";
import { SessionManager } from "./session-manager.ts";

async function main(): Promise<void> {
  const config = loadConfig(process.argv.slice(2));
  const sessions = new SessionManager({
    pathPolicy: new PathPolicy(config.allowedRoots),
    journal: new SessionJournal(config.statePath),
    processFactory: nodePtyProcessFactory,
    adapters: [],
    leaseTtlMs: config.leaseTtlMs,
    maxSnapshotScrollback: config.maxSnapshotScrollback,
  });
  const server = new TerminalHostServer(config, sessions);
  let closing = false;
  const close = async (): Promise<void> => {
    if (closing) {
      return;
    }
    closing = true;
    await server.close();
  };
  process.once("SIGINT", () => void close());
  process.once("SIGTERM", () => void close());
  await server.start();
  process.stdout.write(
    `Terminal Host listening at http://${config.host}:${config.port}\n`,
  );
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? error.stack ?? error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
});
