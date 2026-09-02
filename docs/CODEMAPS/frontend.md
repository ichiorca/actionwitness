# frontend — `apps/actionwitness_service/frontend/src`

> **Paths below are relative to** `apps/actionwitness_service/frontend/src`.

React + strict TypeScript. A **minimal UI and browser-integration layer**: it does
not duplicate Python-owned business transitions, consent, canonical state, or
verdict logic. If you are computing a verdict in TypeScript, stop.

**Start at** `App.tsx` (composition) and `webmcp/adapter.ts` (the WebMCP seam).

## The WebMCP seam

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `webmcp/adapter.ts` | 826 | **Every** `document.modelContext` access | `tests/architecture/test_webmcp_adapter_isolation.py` fails the build if `modelContext` appears anywhere else in product code. Owns registration, StrictMode-safe cleanup (an aborted registration, never a duplicate tool), `AbortSignal` forwarding where the pinned package supports it, and normalizing success *and* thrown errors into `{content, isError}`. Three exported registration paths: `useHarnessTool`, `useNativeTool`, `useRawNativeTool`. |
| `webmcp/identity.ts` | 477 | Tool-identity hashing for the surface witness | Must agree with the Python side's canonicalization, or every run reports a mutation. |
| `webmcp/surface.ts` | 215 | `getTools()` capture and `toolchange` subscription | Polls and subscribes; drops stale results. |
| `tools/harnessTools.ts` | 675 | The 8 harness tools (`list_contract_templates`, `get_outcome_contract`, `arm_outcome_contract`, `verify_outcome`, `get_run_findings`, `reset_workspace`, `create_regression_eval`, `run_regression_eval`) | Which tools exist is a function of workspace phase (§11.5) — the tool set *is* the state machine, surfaced. `tests/architecture/test_harness_tool_surface.py` guards the list. |
| `tools/workspaceStatus.ts` | 76 | `get_workspace_status` | |
| `integrations/buggyStore/tools.ts` | 378 | The 5 target tools | Demo-target vocabulary. Keep it out of the generic workspace UI. |
| `integrations/buggyStore/poisoned.ts` | 101 | The `tool_surface_poisoned` fixture — a look-alike tool registered mid-run | Deliberate misbehavior. Registers through the adapter (`useRawNativeTool`); its *observable* behavior must not change. |

## API boundary

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `api/client.ts` | 193 | The one `fetch` wrapper: `response.ok`, empty/malformed bodies, `AbortSignal`, stable errors | Every caller **must** supply a `parse` validator — there is no default `as T` escape hatch. Exports the narrowing helpers (`requireRecord`, `stringList`, `optionalString`); use them rather than casting. |
| `api/workspace.ts` · `api/contracts.ts` · `api/benchmark.ts` · `api/evals.ts` | 301 · 178 · 212 · 147 | Per-domain parsers | Hand-narrow `unknown`. An `as X` at this boundary is the move the constitution forbids. |

## State and polling

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `state/useWorkspace.ts` | 81 | Workspace polling | |
| `state/useRunTimeline.ts` | 140 | Run timeline polling | Since-cursor paging, **chained** `setTimeout` (never `setInterval`, so a slow response cannot stack requests), a `live` guard before every `setState`, and `AbortController` on unmount. Stale responses are dropped, not applied. |
| `state/confirmations.ts` | 118 | In-page correlation of pending confirmations | |

## Components

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `components/panels.tsx` | 1015 | Findings, timeline, comparison, surface, benchmark panels | **Largest frontend file and a known hotspot.** Split per panel before adding another. Must stay usable with no `modelContext`. |
| `components/ConfirmationDialog.tsx` | 238 | The consent gate | No preselected control; `Tab`/`Shift+Tab` trapped with wrap-around; focus placed on open and restored on close; single-settle cancellation. All of it is tested — including the keyboard path. |
| `components/ContractForm.tsx` | 280 | Template instantiation | |
| `components/GuidanceBanner.tsx` | 144 | "Who acts next" | |
| `components/WorkspaceErrorBoundary.tsx` | 66 | Top-level error boundary, wired in `main.tsx` | Without it a render error blanks the workspace. Logs to console so the release checklist's console check still sees it. |
| `App.tsx` | 655 | Composition, effects, run wiring | |

## Not shipped to users

| Path | Note |
|---|---|
| `spike/` | ADR-0002 research entry point (`spike.html`). Stripped from the production bundle by the Dockerfile; never reachable from the product UI. |
| `test/modelContextDouble.ts`, `test/halfBrokenStorefront.ts` | Deterministic WebMCP doubles and the half-broken storefront fixture (§26.3). |

## Gates

`npm run typecheck` (`tsc --noEmit`) is a **separate gate** from `npm run build` —
the bundler is not a type-checker. `strict`, `exactOptionalPropertyTypes` and
`noUncheckedIndexedAccess` are on; do not weaken them. `e2e/` holds a 14-spec
Playwright lane that is deliberately not release-gating (§26).
