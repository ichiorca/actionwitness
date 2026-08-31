import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the local FastAPI service; production assets are
// served same-origin by the composed deployment (spec §20.1, §29.1).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
});
