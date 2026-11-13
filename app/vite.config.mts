import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the engine runs on DVA_ENGINE_PORT (scripts/dev.mjs) and Vite proxies /api to it.
const enginePort = process.env.DVA_ENGINE_PORT || "8765";

export default defineConfig({
  plugins: [react()],
  base: "./",
  // cssMinify off: Vite's CSS minifier (lightningcss) needs a native add-on that npm sometimes fails to install
  // on Windows; the stylesheet is ~25 KB, so minifying it gains nothing in a desktop app.
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 2000, cssMinify: false },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: `http://127.0.0.1:${enginePort}`, changeOrigin: true } },
  },
});
