import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Separate from vite.config.ts so the test environment cannot drift into the
// production bundle, and so `npm run build` never loads test-only plugins.
//
// jsdom does not supply WebMCP (spec §26.3): `document.modelContext` is absent
// here unless a test installs the deterministic double. That absence is itself
// the unsupported-browser case the adapter must handle, so it is left alone.
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