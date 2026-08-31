# ADR-0002 — WebMCP lifecycle package

- **Status:** Proposed
- **Date:** 2026-08-31
- **Implementing change:** 001-T10 (spike harness and checklist); the pin itself is 001-T11

> **Operator-gated.** This record is `Proposed`, not `Accepted`, and deliberately
> states no verdict. Choosing the package requires a human running the spike
> against the exact target browser build (spec §25.1, §33 open question 2). An
> autonomous session can build the instrument and say what would count as
> evidence; it cannot supply the observation.

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