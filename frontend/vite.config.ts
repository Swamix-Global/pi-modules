import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/dashboard/",
  server: {
    host: "127.0.0.1",
    proxy: Object.fromEntries(
      ["/stats", "/api"].map((path) => [
        path,
        {
          target: "http://127.0.0.1:4141",
          changeOrigin: true,
          configure(proxy) {
            proxy.on("proxyReq", (upstream, request) => {
              if (request.headers.origin === `http://${request.headers.host}`)
                upstream.setHeader("Origin", "http://127.0.0.1:4141");
            });
          },
        },
      ]),
    ),
  },
});
