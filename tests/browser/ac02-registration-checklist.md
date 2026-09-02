# Manual checklist — AC-02, three registration mechanisms

**Status:** awaiting the operator's browser run
**Criterion:** AC-02 — "Given the application page, when inspected in Chrome
DevTools, then the expected native, imperative, and declarative tools are
visible with valid schemas."
**Record results in:** this file, under *Run record*.

This is a manual checklist by design. §26.4 makes WebMCP browser checks a manual
smoke test against a pinned build, and §7.5 makes provisioning a flagged browser
a hard cut — its absence must never fail the release-gating suite.

An agent session cannot complete this. AC-02 is a statement about what a browser
did with the page, and the automated suite runs in jsdom, which has no WebMCP
implementation at all.

---

## What is already proved automatically

Do not re-test these by hand.

**Python** (`uv run pytest -q`):

- `tests/unit/test_template_expansion.py` — a template expands only from the
  scalars it allowlists; an unallowlisted one is *rejected* rather than ignored;
  the quantity moves the price with it; nothing a caller sends becomes a
  contract term.
- `tests/integration/test_contract_instantiation.py` — `POST /contracts` through
  the real service: the created contract is immutable, workspace-scoped,
  selectable and armable, and a body carrying `assertions` is refused outright.
- `tests/architecture/test_harness_tool_surface.py` — `create_outcome_contract`
  is in the server's harness partition, so the browser reporting it in
  `getTools()` does not read as the *target* mutating its tool surface.

**Frontend** (`npm run test`):

- `src/components/contractForm.test.tsx` — every annotation §25.2 requires is on
  the markup; the submit handler calls `preventDefault()`; one handler serves
  both callers and sends the same payload; `respondWith` receives the agent's
  promise and reports a refusal as `isError`; `toolactivated` and `toolcancel`
  are surfaced; a control the template does not accept is disabled and stays out
  of the submission without disappearing from the form.
- `src/webmcp/adapter.test.ts`, `src/spike/nativePath.test.tsx` — the native and
  imperative paths' lifecycle, under StrictMode.

**A jsdom double is not a browser.** Everything above asserts what the page
*offers*. Only a real browser can say what it *registered*, and those can differ
— which is the entire point of the run below.

---

## Setup

1. Record the exact browser build before touching anything:
   - Chrome: `chrome://version` → full version string.
   - Flag state: `chrome://flags/#enable-webmcp-testing`.
2. Start the service and the workspace:

       uv run actionwitness serve
       cd apps/actionwitness_service/frontend && npm install && npm run dev

3. Open **`/`** (the workspace, not `/spike.html`).
4. Open the DevTools WebMCP panel.

---

## Checks

### 1. All three mechanisms are registered at once

| # | Check | Expected | Result |
|---|---|---|---|
| 1.1 | `get_workspace_status` is listed | present, `readOnlyHint: true` | |
| 1.2 | `list_contract_templates` is listed | present | |
| 1.3 | `create_outcome_contract` is listed | present | |

1.1 is the **native** path, 1.2 the **imperative** hook, 1.3 the
**declarative** form. AC-02 needs all three in one page: the mechanisms are not
alternatives, and a build that quietly fell back to one of them would still look
correct in every automated test above.

### 2. The declarative tool's schema came from the markup

| # | Check | Expected | Result |
|---|---|---|---|
| 2.1 | `create_outcome_contract` has an input schema | four parameters | |
| 2.2 | Each parameter carries the form's description text | matches `toolparamdescription` | |
| 2.3 | The description names the limit | "cannot author arbitrary assertions" | |

If 2.1 shows no parameters, the browser did not associate the controls with the
form — check that each control is a descendant of the annotated `<form>`.

### 3. An agent can actually submit it

| # | Check | Expected | Result |
|---|---|---|---|
| 3.1 | Invoke `create_outcome_contract` with `template_id` only | a contract is created and appears in the contract panel | |
| 3.2 | The page did not navigate | the workspace is still mounted, other panels intact | |
| 3.3 | The agent received the created contract, not an empty result | result names a `ctr_…` identifier | |
| 3.4 | Invoke it with `quantity: 99` | the call reports an error naming `quantity` | |

3.3 is the one to watch. Without `respondWith`, the call resolves the instant
the handler returns — which is *before* the contract exists — and the agent
would read a pending request as a finished one. A result that arrives but names
nothing is that failure.

### 4. Agent presence is visible to the person

| # | Check | Expected | Result |
|---|---|---|---|
| 4.1 | While the agent holds the form | "An agent is filling in this form." | |
| 4.2 | Cancel the agent's call mid-form | "The agent cancelled its submission. Nothing was created." | |

§25.2 asks for `toolactivated` and `toolcancel` to be handled "so agent focus
and cancellation remain visible". A form filled in and submitted by something
the person cannot see is precisely the failure this product exists to surface.

### 5. Without WebMCP, the form still works

| # | Check | Expected | Result |
|---|---|---|---|
| 5.1 | Open the page in a browser with no WebMCP | the form renders and submits normally | |
| 5.2 | A contract is created by the human submission | appears in the contract panel | |

Constitution §8: "the complete human workflow remains usable when WebMCP is
absent or registration fails." The declarative form is the mechanism where this
is most likely to be got wrong, because the annotations are inert markup in a
browser that ignores them — which is exactly why it should still be an ordinary
working form.

---

## Run record

| Field | Value |
|---|---|
| Date | |
| Browser build | |
| Flag state | |
| Outcome (pass / fail) | |
| Notes | |

Any failure here is an AC-02 failure and blocks the Tier 3 gate for this item.
Record it rather than adjusting the checks.
