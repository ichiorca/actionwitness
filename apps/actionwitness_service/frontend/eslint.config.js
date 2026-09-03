/**
 * Lint configuration for the harness workspace (constitution §6).
 *
 * The quality bars require that "configured formatting and lint checks pass
 * with no ignored new violations", and until 006 this package had no lint
 * configuration at all — cheap to overlook while the frontend was ~200 lines,
 * and not once it carries the Tier 1 UI.
 *
 * The rules below are the ones the stack rules name specifically rather than a
 * general style pass: unchecked promises, exhaustive switches over discriminated
 * unions, and the hook dependency rules that a stale closure hides behind.
 */
import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "src/generated/**",
      // The browser lane's working directory: assembled bundles, databases,
      // and Playwright traces. Linting a built bundle reports the bundler's
      // output as this project's style.
      ".e2e/**",
    ],
  },
  js.configs.recommended,
  // Type-checked rules need a program, so they apply only to the files the
  // tsconfig actually includes. Applying them to this config file — which no
  // tsconfig covers — is what makes ESLint fail before it lints anything.
  ...tseslint.configs.recommendedTypeChecked.map((config) => ({
    ...config,
    files: ["src/**/*.{ts,tsx}"],
  })),
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      globals: { ...globals.browser, WebMCP: "readonly" },
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    plugins: {
      react,
      "react-hooks": reactHooks,
      "jsx-a11y": jsxA11y,
    },
    settings: { react: { version: "detect" } },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
      // A floating promise in a browser handler is a lost error, and the
      // harness's handlers are all async.
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
      // Untrusted values arrive as `unknown` and are narrowed; an `any` that
      // slipped in would silently switch that off.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unnecessary-condition": "off",
      "react/jsx-key": "error",
    },
  },
  {
    // This file and the Vite configs are linted without type information.
    files: ["*.js", "*.ts"],
    ...tseslint.configs.disableTypeChecked,
  },
  {
    // Node-side tooling: the Playwright configuration, its setup script, and
    // the browser lane's own sources. These are not part of the bundle — they
    // run in Node and drive the built application from outside — so they need
    // Node's globals and none of the React or browser rules.
    //
    // Linted without type information on purpose: the typed rules are bound to
    // `tsconfig.json`, which covers `src/` alone. `npm run typecheck:e2e` is
    // where these files get their strict type coverage, from their own project.
    files: ["e2e/**/*.ts", "playwright*.config.ts", "scripts/**/*.mjs"],
    languageOptions: {
      parser: tseslint.parser,
      // Both, because the lane straddles the boundary: fixtures run in Node,
      // and everything inside `page.evaluate` is browser code compiled into
      // the same file.
      globals: { ...globals.node, ...globals.browser },
      parserOptions: { project: false, projectService: false },
    },
    plugins: { "@typescript-eslint": tseslint.plugin },
    rules: {
      ...tseslint.configs.disableTypeChecked.rules,
      // The base rules cannot see TypeScript's own declarations, so they report
      // every type-only import as an undefined or unused name. Strict
      // `tsc --noEmit -p tsconfig.e2e.json` is the gate that actually catches
      // an unresolved or unused name here.
      "no-undef": "off",
      "no-unused-vars": "off",
      // The one rule worth keeping: this lane narrows tool results and API
      // bodies that arrive as `unknown`, and an `any` would switch that off.
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  {
    files: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/spike/**"],
    rules: {
      // Test doubles legitimately construct shapes the real types forbid, and
      // the spike deliberately loads unresolved specifiers.
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-call": "off",
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-return": "off",
      // A double implementing an async interface has methods with nothing to
      // await. Requiring one would mean adding a meaningless `await` to satisfy
      // a linter rather than to describe the code.
      "@typescript-eslint/require-await": "off",
    },
  },
);
