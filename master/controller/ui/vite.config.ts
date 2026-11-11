import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the FastAPI controller at /app/. `npm run dev` proxies the API to a local controller.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8700", "/login": "http://127.0.0.1:8700" } },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  test: { environment: "node" },
});
