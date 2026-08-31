import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { TerminalHostError } from "../src/errors.ts";
import { resolveExecutablePath } from "../src/runtime.ts";

function createExecutable(directory: string, name: string): string {
  const executable = path.join(directory, name);
  fs.writeFileSync(executable, "test", "utf8");
  if (process.platform !== "win32") {
    fs.chmodSync(executable, 0o755);
  }
  return executable;
}

test("resolves a bare executable from the supplied terminal environment", (context) => {
  const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "terminal-runtime-"));
  context.after(() => fs.rmSync(temporaryDirectory, { recursive: true, force: true }));
  const executableName = process.platform === "win32" ? "agent.exe" : "agent";
  const executable = createExecutable(temporaryDirectory, executableName);

  const resolved = resolveExecutablePath(
    "agent",
    { PATH: temporaryDirectory, PATHEXT: ".EXE" },
    temporaryDirectory,
  );

  assert.equal(resolved.toLowerCase(), executable.toLowerCase());
});

test("keeps an explicit executable path after validation", (context) => {
  const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "terminal-runtime-"));
  context.after(() => fs.rmSync(temporaryDirectory, { recursive: true, force: true }));
  const executable = createExecutable(
    temporaryDirectory,
    process.platform === "win32" ? "agent.exe" : "agent",
  );

  assert.equal(
    resolveExecutablePath(executable, { PATH: "" }, temporaryDirectory),
    executable,
  );
});

test("reports a stable error when the executable is unavailable", () => {
  assert.throws(
    () => resolveExecutablePath("missing-agent", { PATH: "" }, process.cwd()),
    (error: unknown) =>
      error instanceof TerminalHostError &&
      error.code === "terminal.executable_not_found" &&
      error.statusCode === 422,
  );
});
