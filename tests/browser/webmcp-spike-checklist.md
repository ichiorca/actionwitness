# Manual checklist — ADR-0002 WebMCP lifecycle spike

**Status:** awaiting the operator's browser run
**Decides:** which hook package (if either) this project pins — `use-webmcp-tool`
or `usewebmcp` — and whether cancellation-sensitive tools must fall back to
direct native registration.
**Record results in:** `docs/adr/0002-webmcp-lifecycle-package.md`

This is a manual checklist by design. Spec §26.4 makes WebMCP browser checks a
manual smoke test against a pinned build, and §7.5 makes provisioning a flagged
browser a hard cut — its absence must never fail the release-gating suite.

An agent session cannot complete this. It needs a human at a real browser, and
the pin that follows is an operator decision (spec §33 open question 2).

---

## What is already proved automatically

Do not re-test these by hand; they run in `npm test`
(`src/spike/nativePath.test.tsx`) against the deterministic `document.modelContext`
double:

- native registration leaves exactly one tool after a StrictMode double-mount;
- unmount unregisters it, leaving nothing behind;
- the tool is registered with `readOnlyHint: true`;
- the per-invocation `{ signal }` reaches the handler;
- an absent `document.modelContext` reports `unsupported` rather than throwing.

A jsdom double is not a browser. The point of the run below is that the **real**
implementation may differ from the double in exactly the ways that matter.

---

## Setup

1. Record the exact browser build before touching anything:
   - Chrome: `chrome://version` → full version string.
   - Flag/origin-trial state: `chrome://flags/#enable-webmcp-testing`.
   - Or, for ChatGPT's in-app browser, the app build identifier.
2. Start the harness:

       cd apps/actionwitness_service/frontend
       npm install          # NOT npm ci — there is no lockfile until the pin lands
       npm run dev

3. Open **`/spike.html`** (not `/`). The harness mounts under `React.StrictMode`,
   which is the condition the M0 exit gate names.
4. Open the DevTools WebMCP panel.

> Install **one** candidate at a time — `npm install use-webmcp-tool`, run the
> whole checklist, then `npm uninstall` it and repeat for `usewebmcp`. Having both
> present at once makes a duplicate registration ambiguous.
>
> **Do not commit `package-lock.json` during the spike.** The lockfile is
> committed once, after the pin, so it records the tested tree (task 001-T11).

---

## Per-candidate run

Run every row for **each** of: `native` (the control), `use-webmcp-tool`,
`usewebmcp`. Record pass/fail plus what you actually observed — a surprising
observation is more valuable to the ADR than a tick.

| # | Check (spec §25.1) | How | Pass condition |
|---|---|---|---|
| 1 | Unsupported environment no-op | Open in a browser without WebMCP | Page renders, no exception, status reads `absent` |
| 2 | Registration | Select the path | `getTools()` count for `get_workspace_status` is exactly **1** |
| 3 | **StrictMode duplication** | Observe on first mount | Count is **1**, never 2. Any 2 fails this candidate outright |
| 4 | Unmount cleanup | Click *Unmount* | Count drops to **0** |
| 5 | Remount | Click *Mount* | Back to **1**, with no leftover from the previous mount |
| 6 | `enabled` transitions | Switch paths back and forth | Count never exceeds 1; no orphan from the previous path |
| 7 | `getTools()` reconciliation | Click *Re-read getTools()* | The list matches the DevTools panel, not the component's own state |
| 8 | `toolchange` event | Mount/unmount while watching the log | An event fires per change; bursts coalesce rather than dropping |
| 9 | Normalized success output | Invoke the tool from DevTools | Returns the status object; result stays within the 1,500-char budget |
| 10 | Normalized thrown error | Temporarily throw inside `execute` | Surfaces as an `isError: true` envelope, not an unhandled rejection |
| 11 | **Execution signal forwarded** | Invoke, read the Invocations section | `signal forwarded: true`. **If false, this candidate cannot carry `proceed_to_checkout`** (FR-037, LD-4) |
| 12 | Cancellation | Invoke and abort from DevTools | Handler observes `aborted: true` |
| 13 | Registration failure display | Register a tool with an invalid name | Failure is visible in the UI, not swallowed |
| 14 | Descriptions and hints | Inspect `getTools()` output | `readOnlyHint` survives; note whether descriptions are carried |

### If a candidate fails to load

The harness reports `not installed` (expected before you install it) or
`unexpected` with the module's actual exports. If it is `unexpected`, the
package's hook is exported under a name the probe does not recognise: add it to
`HOOK_EXPORT_NAMES` in `src/spike/hookPath.tsx`, or adjust the single marked call
site in `HookProbeInner` if the signature differs. **Record any such adjustment in
ADR-0002** — an awkward API is itself evidence for the decision.

---

## Also record (spec §29.3 pinning table)

- [ ] Exact Chrome version and build, plus flag / origin-trial configuration
- [ ] WebMCP API location actually observed (`document.modelContext`)
- [ ] Whether `document.modelContext.getTools()` exists and what it returns
- [ ] Whether the `toolchange` event fires
- [ ] Whether returned definitions carry descriptions and side-effect hints —
      **if either is missing, `stable_tool_surface` is visibly disabled rather
      than reported as passing** (spec §29.3, §33 q10)
- [ ] Selected hook package **and its exact tested version**
- [ ] Tested `webmcp-types` version (currently resolves to 0.1.5 from `"*"`)
- [ ] Any `compat.d.ts` augmentation the tested build needed

---

## Decision to record in ADR-0002

1. Which package is pinned, at which exact version — or that **neither** is, and
   direct native registration is used throughout.
2. Whether the pinned hook forwards the execution context. If it does not,
   ADR-0002 must state that cancellation-sensitive tools use direct native
   registration, and the adapter must enforce that split.
3. Anything observed that contradicts the jsdom double, so
   `src/test/modelContextDouble.ts` can be corrected. A double that disagrees
   with the real browser is worse than no double at all.

## After the decision (task 001-T11)

- [ ] Flip ADR-0002 to `Accepted` and update the `docs/adr/README.md` docket row
- [ ] Pin the chosen hook and `webmcp-types` in `package.json`
- [ ] Commit `package-lock.json`
- [ ] Fill the README §29.3 pinning table
- [ ] Replace the guard in
      `tests/architecture/test_frontend_command_surface.py` that currently
      asserts *no* hook is pinned with one asserting *exactly one* is
