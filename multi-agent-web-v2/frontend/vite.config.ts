import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const DEFAULT_DEV_PROXY_TARGET = "http://127.0.0.1:8021";

export function resolveDevProxyTarget(
  environment: NodeJS.ProcessEnv = process.env,
): string {
  return (
    environment.MULTI_AGENT_WEB_V2_DEV_PROXY_TARGET?.trim() || DEFAULT_DEV_PROXY_TARGET
  );
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
    proxy: {
      "/api": resolveDevProxyTarget(),
      "/health": resolveDevProxyTarget(),
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true
  }
});
