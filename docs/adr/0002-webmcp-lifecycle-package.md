# ADR-0002 — WebMCP lifecycle package

- **Status:** Accepted
- **Date:** 2026-08-31
- **Implementing change:** 001-T10 (spike harness and checklist); pin recorded in 001-T11
- **Verdict:** pin `use-webmcp-tool@0.2.0` + `webmcp-types@0.1.5`; cancellation-sensitive
  tools use direct native registration (rule 3 split, binding on the adapter)

> The spike ran against the operator's real browser (Claude-driven, operator-
> consented session grants; the operator enabled the flag and confirmed the pin).
> Results below; the decision is a mechanical application of the rule fixed in
> advance.

## Spike results (2026-08-31)

**Environment:** Chrome 151.0.0.0 stable (Windows), `#enable-webmcp-testing`
Enabled. Before the flag: `document.modelContext` absent, page degraded
correctly (check 1 pass). After: the API exists on **both**
`document.modelContext` and `navigator.modelContext`, surface
`registerTool` / `getTools` / `executeTool` / `ontoolchange`.

| Check | native | use-webmcp-tool@0.2.0 | usewebmcp@5.1.0 |
|---|---|---|---|
| StrictMode count stays 1 (rule 1) | pass | pass | pass |
| Unmount → 0 / remount → 1 | pass | pass | pass |
| `toolchange` fires | pass (per-surface; bursts NOT coalesced, none dropped) | pass | pass |
| Invoke result (size) | raw object (89 ch) | MCP content envelope (147 ch) | content + `structuredContent` + `isError:false` (262 ch) |
| **Signal forwarded (rule 2)** | **false** | **false** | **false** |
| Descriptions + `readOnlyHint` | pass (also `untrustedContentHint`) | pass | pass |

**Rule application:** rule 1 rejects neither → rule 2 cannot separate them
(no path forwards the signal — see below) → rule 3: pin the otherwise-better
candidate; the runnable checks tied → rule 5 tie-break: **`use-webmcp-tool`**,
the challenge-linked baseline.

**The sharper finding under rule 3:** in this build the *native control* also
receives no per-invocation signal via `executeTool` — the ceiling is the
browser's, not either hook's. The rule-3 split (cancellation-sensitive tools
via direct native registration) is recorded as binding on the adapter, and
additionally: no Tier 1 design may treat the per-invocation `AbortSignal` as
a safety mechanism in the pinned build. The server-side confirmation binding
with atomic single consumption (spec §14, FR-037's real enforcement point)
carries cancellation safety; the signal, when a future build forwards it, is
a responsiveness improvement only.

**Build-observed API facts (spec §29.3 table):**
- `executeTool(tool, args)` — arity 2; `tool` is the descriptor object from
  `getTools()` (not a name); `args` must be a JSON **string** (`"{}"`);
  a third options/signal argument is silently ignored.
- `registerTool(...)` returns a **Promise**; an invalid tool name is accepted
  at call time (validation, if any, is asynchronous).
- `getTools()` descriptors carry `description`, `inputSchema`, `annotations`
  (`readOnlyHint`, `untrustedContentHint`), `origin`, `title`, `window` —
  `stable_tool_surface` is viable in this build.
- Known divergences from `src/test/modelContextDouble.ts` (kept, documented,
  not yet reconciled): the double forwards `{ signal }` (build does not), models
  `registerTool` synchronously (build returns a Promise), and lacks
  `executeTool`. Reconcile when M5 wires real invocation paths.

**Harness corrections made during the run (per checklist instruction):**
- `hookPath.tsx` imported candidates with a bare specifier under
  `@vite-ignore`, which reaches the browser unresolved and can never load; in
  dev the specifier now routes through Vite's `/@id/` resolution endpoint.
- Both candidates export the hook as `useWebMCP`, which `HOOK_EXPORT_NAMES`
  did not list; added.

## Context

Application components must never import a WebMCP package directly. Everything
package-specific is isolated behind
`apps/actionwitness_service/frontend/src/webmcp/adapter.ts` (spec §25.1), so the
choice below is confined to one module — but it is still load-bearing, because
the hook determines what the adapter *can* offer.

Two candidates:

- **`use-webmcp-tool`** — the GoogleChromeLabs package the challenge resources
  link, and the Devpost baseline named in spec §19.1.
- **`usewebmcp`** — the package referenced by the current Chrome imperative-API
  guide.

Both are experimental, as is WebMCP React support generally. Exactly one may be
pinned (spec §32 locked decision 4).

Two properties decide it, and only one is about convenience:

1. **StrictMode lifecycle correctness.** React StrictMode intentionally mounts,
   unmounts and remounts effects. A hook that registers on mount without
   unregistering on cleanup leaves a duplicate tool. The M0 exit gate names this
   directly: the selected path must register and clean up one read-only tool
   "without StrictMode duplication".
2. **Execution-context forwarding.** `webmcp-types` declares
   `execute(input, { signal })`. A hook that swallows that `signal` cannot carry
   a cancellation-sensitive tool. FR-037 makes forwarding mandatory for
   `proceed_to_checkout`, and explicitly forbids simulating cancellation by
   hiding the confirmation UI.

Property 2 has a known escape hatch — locked decision 4 and FR-037 both allow
direct native registration for cancellation-sensitive tools — so a hook that
fails it is not disqualified outright, only narrowed. Property 1 has no escape
hatch: a duplicate registration is a correctness failure.

## Decision

**Pending the operator's browser run.** What this change establishes is the
instrument and the decision rule.

### The decision rule, fixed in advance

Fixing the criteria before seeing results is the point; it stops the pin from
being rationalised after the fact.

1. A candidate that **duplicates a registration under StrictMode is rejected**,
   with no further consideration.
2. Among candidates that survive, **prefer the one that forwards the execution
   context**. That capability cannot be added from outside the package.
3. If neither forwards it, pin the otherwise-better candidate and route
   cancellation-sensitive tools through direct native registration, recording
   that split as binding on the adapter.
4. If **both** fail rule 1, pin neither and use direct native registration
   throughout. The native path is already implemented and tested, so this is a
   genuine option rather than a failure state.
5. Ties break toward `use-webmcp-tool`, the challenge-linked baseline (§19.1).

### What has been built

- **`src/spike/`** — a harness at `/spike.html`, a separate Vite entry so a
  decision tool can never leak into the workspace UI. It mounts under
  `React.StrictMode`, switches between `native`, `use-webmcp-tool` and
  `usewebmcp`, registers one read-only `get_workspace_status` stub, and reports
  the live tool count from `document.modelContext.getTools()` — the browser's own
  view, never the component's state, because reconciling from component state is
  one of the bugs being looked for.
- **Neither candidate is a dependency.** They are loaded through a runtime
  specifier Vite is told not to resolve, so the harness builds and ships with
  neither installed and reports each as "not installed" until the operator adds
  one. That is what keeps this record honestly `Proposed`.
- **`src/test/modelContextDouble.ts`** — the deterministic `document.modelContext`
  double spec §26.3 requires, implementing the `webmcp-types` surface with no
  timers or randomness. It counts `registerCalls` separately from surviving
  tools, because a correct adapter *does* call `registerTool` twice under
  StrictMode while leaving one tool: counting only survivors would hide a leak,
  counting only calls would report a false one.
- **`src/spike/nativePath.test.tsx`** — the native control path already passes
  the exit-gate condition automatically: one tool after a double-mount, none
  after unmount, `readOnlyHint` preserved, signal forwarded, and unsupported
  environments reporting rather than throwing.
- **`tests/browser/webmcp-spike-checklist.md`** — the fourteen-row manual run,
  the §29.3 pinning table to fill, and the T11 follow-up steps.

## Consequences

### Positive

- The decision rule is written before the data, so the pin is an application of
  stated criteria rather than a post-hoc justification.
- The native control path is implemented and tested now, which means rule 4 is a
  real fallback rather than a cliff: if both hooks fail, the project already has
  a working path.
- Building the harness with neither package installed keeps `package.json` and
  the (absent) lockfile honest — nothing records a choice that has not been made.
- The `modelContext` double is reusable well past this decision; every Tier 1
  frontend lifecycle test in §26.3 needs it.

### Negative

- **A jsdom double is not a browser.** Everything automated here proves the
  adapter behaves against a model of the API, and the real implementation can
  differ precisely where it matters. The manual run is therefore load-bearing,
  and the checklist asks explicitly for observations that contradict the double
  so it can be corrected.
- The harness's hook call site is written against an *assumed* signature. Both
  candidates' APIs are unverified until installed, so the operator may have to
  adjust one marked line. This is inherent to spiking an unpinned dependency, but
  it means the harness is not fully proven until first use.
- Keeping the spike in the repository is ongoing surface: a second HTML entry
  point and a directory that is not product code. **Follow-up, owed at M5:**
  decide whether `src/spike/` is deleted once the adapter is complete, or kept as
  a regression instrument for future browser builds.
- This project is now exposed to an experimental package's release cadence in a
  place with no upstream stability guarantee. Pinning an exact tested version is
  the mitigation, and it is a weak one.

## Rejected alternatives

### Pinning `use-webmcp-tool` now because it is the challenge baseline

Rejected: it is the *tie-breaker*, not the criterion. Pinning before the spike
would mean discovering a StrictMode duplication or a dropped execution signal
after the adapter, the tools, and the confirmation flow were built on it — and
FR-037's cancellation requirement is not something to find late.

### Supporting both packages behind the adapter

Rejected: locked decision 4 requires exactly one. Two experimental
implementations would double the lifecycle surface under test to hedge a decision
that a day's spike resolves, and the adapter's value is that there is one path to
reason about.

### Skipping the hooks and using direct native registration only

Not rejected — it is outcome 4 of the decision rule, and the native path is
already built and tested. It is not adopted *pre-emptively* because a working
hook removes hand-written lifecycle code from every future tool, and hand-rolled
registration lifecycle is exactly where duplicate-registration bugs live.

### Deciding from the packages' documentation instead of running them

Rejected: both are experimental, and the specific failure modes at issue —
StrictMode double-mount behavior and execution-context forwarding — are the ones
documentation is least likely to describe accurately. Spec §29.3 requires a
*tested* version, not a documented one.

## Notes

`webmcp-types` currently resolves from `"*"` to 0.1.5. Its declarations were used
as the authoritative API shape for the harness and the double. Pinning its exact
tested version is part of T11.