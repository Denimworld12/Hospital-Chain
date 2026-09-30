import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const repo = fileURLToPath(new URL("..", import.meta.url));
const api = process.env.MEDCHAIN_DASHBOARD_API || "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 900 },
  resolve: {
    alias: {
      "@assets": `${repo}assets/svg`,
      "@design": `${repo}design`,
    },
  },
  server: {
    fs: { allow: [repo] },
    proxy: {
      "/api": api,
      "/ws": { target: api.replace(/^http/, "ws"), ws: true },
    },
  },
});
