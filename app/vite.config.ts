import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the engine runs on DVA_ENGINE_PORT (scripts/dev.mjs) and Vite proxies /api to it.
const enginePort = process.env.DVA_ENGINE_PORT || "8765";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 2000 },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: `http://127.0.0.1:${enginePort}`, changeOrigin: true } },
  },
});
