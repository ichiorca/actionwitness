import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Separate from vite.config.ts so the test environment cannot drift into the
// production bundle, and so `npm run build` never loads test-only plugins.
//
// jsdom supplies no `document.modelContext`, which is exactly right here: this
// storefront must work in a browser with no WebMCP support at all, so the
// absence is the condition under test rather than something to polyfill.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
  },
});
