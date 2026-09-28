import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Served by the ctx-engine router at /dashboard/ with assets requested from /dashboard/assets/.
// The API lives at the site root (/stats, /request/<id>, /token-stats, /config, /api/login …) and is
// proxied to the loopback router during `npm run dev`.
const API_TARGET = process.env.VITE_API_TARGET ?? "http://127.0.0.1:4141";

export default defineConfig({
  base: "/dashboard/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ["/stats", "/request", "/token-stats", "/config", "/provider", "/api"].map((path) => [
        path,
        { target: API_TARGET, changeOrigin: true },
      ]),
    ),
  },
});
