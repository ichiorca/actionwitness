import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The dev server proxies the store's own versioned surface to the standalone
// store process (spec §15.5). In the composed deployment these assets are served
// same-origin under /demo, so the same relative paths work with no rewrite.
//
// There is deliberately no /api proxy: this storefront never calls the harness.
export default defineConfig({
  server: {
    proxy: { "/demo": "http://localhost:8001" },
  },
  plugins: [react()],
});
