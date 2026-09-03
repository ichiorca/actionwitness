# frontend — `apps/actionwitness_service/frontend/src`

> **Paths below are relative to** `apps/actionwitness_service/frontend/src`.

React + strict TypeScript. A **minimal UI and browser-integration layer**: it does
not duplicate Python-owned business transitions, consent, canonical state, or
verdict logic. If you are computing a verdict in TypeScript, stop.

**Start at** `App.tsx` (composition) and `webmcp/adapter.ts` (the WebMCP seam).

## The WebMCP seam

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `webmcp/adapter.ts` | 895 | **Every** WebMCP access, via one `resolveModelContext()` that tries `document.modelContext` then `navigator.modelContext` (ADR-0002 saw both) | `tests/architecture/test_webmcp_adapter_isolation.py` fails the build if `modelContext` appears anywhere else in product code. Owns registration, StrictMode-safe cleanup (an aborted registration, never a duplicate tool), `AbortSignal` forwarding where the pinned package supports it, and normalizing success *and* thrown errors into `{content, isError}`. Four exported registration paths: `useHarnessTool`, `useNativeTool`, `useRawNativeTool`, and `useDeclarativeTool` (the form-markup tool `ContractForm.tsx` rides on). |
| `webmcp/identity.ts` | 477 | **Invocation-time** tool-identity hashing (FR-169/AC-25): the page declares what it *thinks* it is calling, alongside each `:invoke` | Consumed by `integrations/buggyStore/tools.ts`, **not** by `surface.ts` — the witness deliberately sends definitions, never hashes. Must agree with the Python side's canonicalization; a mismatch is **refused by the server at invoke time** (fail-closed), never silently recorded. |
| `webmcp/compat.d.ts` | — | Declares `navigator.modelContext`, which the pinned `webmcp-types` types on `Document` alone | Tested augmentations only (§25.12) — ADR-0002 observed this location. Allowlisted in the isolation gate as a *declaration*, not access. |
| `webmcp/auditCollector.ts` | 95 | The collector script text an operator runs **on an audited storefront** | Emits a string; performs no access. Allowlisted in the isolation gate for that reason, and kept in its own module so the component that displays it stays covered. FR-162's never-invoked tools are filtered out here, not by the reader. |
| `webmcp/surface.ts` | 215 | `getTools()` capture and `toolchange` subscription | Polls and subscribes; drops stale results. |
| `tools/harnessTools.ts` | 1131 | The 15 hook-registered harness tools: §11.1's eight (`list_contract_templates`, `get_outcome_contract`, `arm_outcome_contract`, `verify_outcome`, `get_run_findings`, `reset_workspace`, `create_regression_eval`, `run_regression_eval`) and §32's seven reads beyond it (`get_run_timeline`, `get_run_comparison`, `list_regression_evals`, `list_audit_packs`, `get_audit_report`, `list_benchmarks`, `get_benchmark_summary`) | Which tools exist is a function of workspace phase (§11.5) — the tool set *is* the state machine, surfaced. `tests/architecture/test_harness_tool_surface.py` guards the list against `surface_service.HARNESS_TOOL_NAMES`. The two audit tools additionally follow the server's **module report** and register nothing while it is off, which `test_cut_feature_hygiene.py` requires. The module header names what stays unpublished and why — every remaining mutation is one §7.5 reserves to a person, or one that would let the subject of a check author its own evidence. |
| `tools/workspaceStatus.ts` | 76 | `get_workspace_status` | |
| `integrations/buggyStore/tools.ts` | 378 | The 5 target tools | Demo-target vocabulary. Keep it out of the generic workspace UI. |
| `integrations/buggyStore/poisoned.ts` | 101 | The `tool_surface_poisoned` fixture — a look-alike tool registered mid-run | Deliberate misbehavior. Registers through the adapter (`useRawNativeTool`); its *observable* behavior must not change. |

## API boundary

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `api/client.ts` | 193 | The one `fetch` wrapper: `response.ok`, empty/malformed bodies, `AbortSignal`, stable errors | Every caller **must** supply a `parse` validator — there is no default `as T` escape hatch. Exports the narrowing helpers (`requireRecord`, `stringList`, `optionalString`); use them rather than casting. |
| `api/audit.ts` · `api/benchmark.ts` | 168 · 472 | The audit and benchmark clients | Both were parser-only for a while; the fetch functions are what made those features reachable. `importEvaluatorReport` sends `rawBody` so FR-117's cap measures the operator's own bytes. `freezeBenchmarkVariants` posts an approval and never an actor — the server owns who may approve. `generateIntentVariants` names no model and no endpoint: which backend the harness talks to is server-controlled configuration. |
| `api/workspace.ts` · `api/contracts.ts` · `api/evals.ts` | 398 · 178 · 147 | Per-domain parsers | Hand-narrow `unknown`. An `as X` at this boundary is the move the constitution forbids. |
| `api/shopify.ts` | 189 | §15.7's pairing: create, and read state plus normalized cart evidence | The status endpoint returns **no raw credential**, so no parser here has a field that could carry one — the launch URL is the single exception, and it is the secret rather than a link to decorate. Money stays a fixed-scale string (FR-113); parsing it back into a number would undo the representation. |

## State and polling

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `state/useWorkspace.ts` | 81 | Workspace polling | |
| `state/useRunTimeline.ts` | 140 | Run timeline polling | Since-cursor paging, **chained** `setTimeout` (never `setInterval`, so a slow response cannot stack requests), a `live` guard before every `setState`, and `AbortController` on unmount. Stale responses are dropped, not applied. |
| `state/confirmations.ts` | 118 | In-page correlation of pending confirmations | |

## Components

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `components/AuditSection.tsx` | 300 | The §12.17 audit journey: authorize → choose pack → collect → report | The module-off state renders as a statement with its reason, never a form that would refuse on submit. |
| `components/BenchmarkSection.tsx` | 488 | Suite picker, create, the evaluator-report file input, and FR-100's frozen intent manifest | Kept out of `panels.tsx` on purpose; it composes `BenchmarkPanel` rather than living inside it. The freeze block renders the *server's* record of the seal, never what it just sent; the variant kinds come from the generated registry; and the `live_evaluator` module gates nothing here — it only says whether a model drafted the texts, so hiding the control behind it would put FR-100 back out of reach. "Draft variants with the model" fills the rows **unticked**: a pre-approved draft would record a decision nobody made, and the checkbox is where the person makes it. With the module off the control is disabled *and* says so in a sentence — §8.4 forbids the greying-out being the only signal. |
| `components/panels.tsx` | 1042 | Twelve presentational panels: `CapabilityBar`, `ToolRegistrationPanel`, `ConfigPanel`, `ContractPanel`, `TargetPanel`, `RunTimeline`, `FindingsPanel`, `UndeclaredChangesPanel`, `ToolSurfacePanel`, `ComparisonPanel`, `EvalPanel`, `BenchmarkPanel` | **Largest frontend file and a known hotspot.** Split per panel before adding another. Must stay usable with no `modelContext`. Scenario modes and fault profiles are adapter-published via the workspace payload — nothing here hard-codes them. `ConfigPanel` + `ToolRegistrationPanel` are the whole Administration view. |
| `components/ConfirmationDialog.tsx` | 238 | The consent gate | No preselected control; `Tab`/`Shift+Tab` trapped with wrap-around — the trap's selector deliberately includes `summary`, or the raw-JSON disclosure would let `Shift+Tab` walk out; focus placed on open and restored on close; single-settle cancellation. `ExpiryCountdown` ticks a 1 s interval and is deliberately **not** `aria-live`; the consequence renders as labelled rows with the verbatim payload behind a disclosure. All tested, including the keyboard path. |
| `components/ContractForm.tsx` | 300 | Template instantiation — the declarative WebMCP tool (`create_outcome_contract`): the browser reads it off the form's own markup | Must stay mounted whichever view is showing, or the tool surface changes shape with navigation. |
| `components/ShopifyPairingPanel.tsx` | 512 | The §12.12 pairing journey: create a pairing for the selected contract, hand the launch URL to a storefront tab, and show §16.5's state, expiry, actor, next action and normalized cart evidence | **`PAIRING_GUIDANCE` is duplicated in `shopify_bridge/actionwitness-bridge.js` on purpose** — §14 requires the harness tab and the storefront tab to agree, and two origins cannot share a module; `shopifyBridge.test.ts` fails when the two copies drift. The launch URL carries a one-time credential in its fragment, so it lives in a ref, never in state and never in the DOM. Every state carries a recovery instruction, terminal ones included. |
| `components/GuidanceBanner.tsx` | 151 | "Who acts next", plus the "Go to this step" walk (exports `goToAction`) | The flash class is removed on `animationend`, never a timer; smooth scroll defers to `prefers-reduced-motion`; an `onGo` override lets `App` bring the owning view forward before focus. |
| `components/WorkspaceErrorBoundary.tsx` | 66 | Top-level error boundary, wired in `main.tsx` | Without it a render error blanks the workspace. Logs to console so the release checklist's console check still sees it. |
| `App.tsx` | 1094 | Composition, effects, run wiring — and the three-view shell: a left `nav.sidebar` splits a Workflow view (`Stage` sections `stage-contract/run/verdict/regression/benchmark`) from an Audit view (`stage-audit`) and an Administration view (`stage-administration`) | **Every view stays mounted; the inactive ones are `hidden`** — unmounting would change the WebMCP surface because a person navigated. `STAGE_OF_PHASE` and `ACTION_TARGET_IDS` are presentation lookups with deliberate `null` fallthrough (FR-120: the server owns "what next") — do not make them exhaustive. `goTo` must switch view *before* focusing, or the walk lands on a hidden control. |
| `styles.css` | 1066 | The whole design system: tokens, light/dark, rail, stages, panels, dialog scrim, status tints | Presentation only, and its header is a contract: never hide/reorder/disable a server-offered control, never make colour the only status channel (§8.4), never depend on DOM order — the e2e locators are role + accessible name. |

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
