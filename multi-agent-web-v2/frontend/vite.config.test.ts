import { describe, expect, it } from "vitest";
import { resolveDevProxyTarget } from "./vite.config";

describe("Vite development proxy", () => {
  it("uses the local Web/BFF default when startup did not inject a target", () => {
    expect(resolveDevProxyTarget({})).toBe("http://127.0.0.1:8021");
  });

  it("uses the Web/BFF target injected by the startup script", () => {
    expect(
      resolveDevProxyTarget({
        MULTI_AGENT_WEB_V2_DEV_PROXY_TARGET: "http://127.0.0.1:8121",
      }),
    ).toBe("http://127.0.0.1:8121");
  });
});
