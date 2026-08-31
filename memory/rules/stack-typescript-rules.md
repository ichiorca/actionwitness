---
title: TypeScript coding rules
scope: project
---

- MUST keep TypeScript at the React/browser/WebMCP boundary; NEVER duplicate Python-owned business transitions, consent, canonical state, outcome evaluation, or idempotency.
- MUST preserve `strict: true`; MUST treat HTTP JSON, WebMCP arguments/results, storage, URLs, messages, and environment values as `unknown` and validate or narrow them before use. NEVER use `any`, `@ts-ignore`, double assertions, non-null assertions, or `as DTO` to bypass a trust boundary.
- MUST model workspace, request, confirmation, registration, and finding lifecycles as discriminated unions with exhaustive `never` checks; MUST preserve action codes, statuses, tool names, and schemas with literal types and `satisfies`.
- MUST distinguish an absent optional property from an explicit `undefined` and guard every indexed lookup; `exactOptionalPropertyTypes` and `noUncheckedIndexedAccess` are not enabled, so compiler silence is NEVER proof that a key exists.
- MUST use `import type` for type-only edges; NEVER import Buggy Store/product semantics into the generic harness UI or create runtime dependencies merely to share a type.
- MUST keep all direct WebMCP access inside `src/webmcp/adapter.ts`. While `registerHarnessTool()` throws and no hook/browser/type-package combination is pinned, MUST expose WebMCP as unsupported; if/when wired, MUST pin one integration path, feature-detect callable methods, validate execute inputs in code, forward cancellation, unregister exact registrations, and set annotations from actual behavior.
- MUST use Effects only to synchronize external systems; MUST implement symmetric setup/cleanup and cancel or reject stale async completions so StrictMode setup → cleanup → setup leaves one tool, listener, timer, or poller. NEVER hide duplicate work with a ref flag.
- MUST route every mutation through the recorded harness API with stable workspace, run, and request identities; NEVER declare business success from an HTTP status or WebMCP result—refresh and render the server-owned observation and verdict.
- MUST centralize relative `/api` requests with `response.ok`, empty/malformed-body handling, runtime validation, stable errors, and `AbortSignal`; NEVER hardcode the Vite proxy target in application code or place secrets in `VITE_` variables.
- MUST treat `npm run build` as bundling only; NEVER claim type-check or lint coverage because the package declares neither. If/when static analysis is added, MUST add separate `tsc --noEmit` and ESLint flat-config scripts covering TS/TSX with typed linting.

<!-- sources fetched at generation: https://eslint.org/docs/latest/use/configure/configuration-files, https://typescript-eslint.io/getting-started/typed-linting/, https://www.typescriptlang.org/tsconfig/strict.html, https://www.typescriptlang.org/docs/handbook/2/narrowing.html, https://www.typescriptlang.org/tsconfig/exactOptionalPropertyTypes.html, https://www.typescriptlang.org/tsconfig/noUncheckedIndexedAccess.html, https://react.dev/reference/react/StrictMode, https://react.dev/reference/react/useEffect, https://vite.dev/guide/features#typescript, https://vite.dev/guide/env-and-mode, https://developer.chrome.com/docs/ai/webmcp/imperative-api, https://developer.chrome.com/docs/ai/webmcp/best-practices, https://developer.chrome.com/docs/ai/webmcp/secure-tools -->
