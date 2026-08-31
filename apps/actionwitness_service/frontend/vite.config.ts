import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server proxies /api to the local FastAPI service; production assets are
// served same-origin by the composed deployment (spec §20.1, §29.1).
//
// The ADR-0002 spike is a second entry point rather than a route inside the
// workspace UI, so a decision tool can never leak into the product surface.
// Open it at /spike.html during `npm run dev`.
//
// Inputs are given as paths relative to `root`; using node:path here would drag
// @types/node into a config that needs nothing from Node.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        spike: "spike.html",
      },
    },
  },
});