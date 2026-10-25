import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// One setting for each side, shared with the backend: LAZYDESK_PORT (backend, default 8000) and LAZYDESK_FRONTEND_PORT (this dev
// server, default 5173). Only these two values reach the app; no other environment variable is exposed to the bundle.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const frontendPort = env.LAZYDESK_FRONTEND_PORT ?? "5173";
  if (!/^[0-9]{1,5}$/.test(frontendPort) || Number(frontendPort) < 1 || Number(frontendPort) > 65535) {
    throw new Error("LAZYDESK_FRONTEND_PORT must be a whole number from 1 to 65535.");
  }
  return {
    plugins: [react()],
    server: { port: Number(frontendPort), strictPort: true },
    define: { __LAZYDESK_PORT__: JSON.stringify(env.LAZYDESK_PORT ?? "8000") },
  };
});
