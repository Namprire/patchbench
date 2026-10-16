import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Remote SSH can forward the VM's loopback socket, so dashboard evidence is
    // not exposed directly on every VM network interface.
    host: "127.0.0.1",
    // Keep the demo off Vite's conventional 5173 port because remote editors
    // commonly retain stale forwards for that address across VM sessions.
    port: 5180,
    strictPort: true,
    // Keep browser traffic on the forwarded frontend origin. Vite reaches the
    // loopback-only Python service inside the VM, including streaming events.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: false,
        headers: { Origin: "http://localhost" },
      },
    },
  },
  build: {
    chunkSizeWarningLimit: 550,
    rollupOptions: { output: { manualChunks: (id) => id.includes("/node_modules/recharts/") ? "charts" : undefined } },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
  },
});
