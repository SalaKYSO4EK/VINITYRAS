import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";

export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    proxy: {
      "/api": { target: process.env.NEXUS_BACKEND_URL || "http://127.0.0.1:8081", changeOrigin: true },
    },
  },
});
