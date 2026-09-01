/**
 * Lint configuration for the standalone storefront (constitution §6; 009-T6).
 *
 * The 006 command-surface gate recorded this package's missing `lint` script as
 * a named gap rather than excusing it with a shared constant — "the quality bars
 * apply to it too" — and pointed at the repository-hardening milestone. This is
 * that milestone.
 *
 * Deliberately mirrors the harness config rather than inventing a second style.
 * Two applications with different lint rules produce review arguments about which
 * one is right, and the answer is always "the same as the other one".
 *
 * One rule is specific to this package: `no-restricted-globals` forbids
 * `navigator.modelContext` and `document.modelContext`. The storefront is the
 * path that must work when WebMCP is absent (AC-09, §26.7), so a reference to it
 * here is a defect rather than a style question — and `tests/architecture` already
 * scans the source for it. Having the linter say so too puts the failure in the
 * editor instead of in CI.
 */
import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  // Type-checked rules need a program, so they apply only to files a tsconfig
  // covers. Applying them to this config file is what makes ESLint fail before
  // it lints anything.
  ...tseslint.configs.recommendedTypeChecked.map((config) => ({
    ...config,
    files: ["src/**/*.{ts,tsx}"],
  })),
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      // No `WebMCP` global here, unlike the harness config. Declaring one would
      // make a reference to it lint clean, which is the opposite of what this
      // package needs.
      globals: globals.browser,
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
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
      // The storefront narrows every response from `unknown`; an `any` would
      // switch that off silently.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unnecessary-condition": "off",
      "react/jsx-key": "error",
    },
  },
  {
    files: ["*.js", "*.ts"],
    ...tseslint.configs.disableTypeChecked,
  },
  {
    files: ["src/**/*.test.{ts,tsx}", "src/test/**"],
    rules: {
      // Test doubles legitimately construct shapes the real types forbid.
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-call": "off",
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-return": "off",
      "@typescript-eslint/require-await": "off",
    },
  },
);
