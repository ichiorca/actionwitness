# Judge demo — Three Minutes, One Lie

The whole demonstration is one contradiction:

> Every tool call reports success. The cart never changed.

Open on that contradiction. Explain the machinery only after the viewer has felt
why it matters.

## Before recording or presenting

- Warm <https://actionwitness.onrender.com/healthz>. A healthy response reports
  `database: ok`; the free deployment can take roughly 30 seconds to wake.
- Open the [workspace](https://actionwitness.onrender.com) and
  [storefront](https://actionwitness.onrender.com/demo) side by side.
- Use a WebMCP-capable browser. If it is unavailable, use the checked recording
  and screenshots; do not describe a replay or mock as live.
- Start from a fresh workspace. Select `pre_fix`,
  `discount_reported_but_not_applied`, and the
  `one-mug-save20-no-checkout` contract.
- Confirm the expected `$20.00` and observed `$25.00` values remain readable at
  the final recording size.

## The under-three-minute narration

### 0:00–0:18 — Cold open

**On screen:** the timeline showing successful tool calls beside the unchanged
storefront cart.

**Say:**

> Search succeeded. Add to cart succeeded. The discount also says success. But
> the cart is still twenty-five dollars. That gap is what ActionWitness catches.

Do not name every component yet.

### 0:18–0:32 — Name the product

**On screen:** the ActionWitness title card, then the shared workspace.

**Say:**

> ActionWitness independently verifies what WebMCP actions actually did. The
> response is evidence; real business state decides the verdict.

### 0:32–1:16 — Run the false-success journey

**On screen:** arm the prepared contract. Ask the agent to search for a mug, add
one, apply `SAVE20`, verify, and show the findings. Stop before checkout.

**Say:**

> A person defines the outcome: one mug, a twenty-dollar total, and no checkout.
> The agent works through the page's recorded WebMCP tools. ActionWitness captures
> every call, then reads the cart through a separate observation path.

Land on the failed `discounted-total` finding and pause.

> The tool claimed the discount landed. The independent observer found twenty-five
> dollars. The run fails as a false success even though the execution and journey
> shape were otherwise correct.

### 1:16–1:48 — Human consent

**On screen:** a prepared `proceed_to_checkout` request waiting on the consent
dialog.

**Say:**

> For a consequential action, the agent cannot continue on its own. The server
> binds approval to this workspace, this run, this action, these arguments, and an
> expiry. The call waits until a person approves once or denies it.

Approve or deny; either is valid. The point is that the person owns the decision.

### 1:48–2:18 — Turn the failure into proof

**On screen:** create the regression case and show the replay result.

**Say:**

> A failed run becomes a portable regression case. It restores the original
> fixture, preserves the source classification, and proves that the failure still
> reproduces—or that the current implementation now passes.

### 2:18–2:38 — Show why the architecture matters

**On screen:** `docs/assets/architecture-at-a-glance.png`.

**Say:**

> Tool report and authoritative observation stay separate all the way through.
> The core is deterministic and target-neutral. An LLM is never the judge of
> business truth.

### 2:38–2:50 — Close

**On screen:** the false-success verdict, then the live URL.

**Say:**

> Your agent says done. ActionWitness checks what actually happened—and turns the
> gap into a test that never has to surprise you twice.

End early rather than filling dead air.

## Failure fallbacks

- **Free deployment wakes slowly:** use the recorded video while `/healthz` warms.
- **WebMCP is unavailable:** show the recorded journey and the deterministic
  integration test. State the limitation plainly.
- **Agent invocation stalls:** cut directly to the captured verdict; do not debug
  on camera.
- **Consent request expires:** reset the workspace and use the prepared consent
  screenshot.
- **Live deployment is unavailable:** run the Docker image locally or present the
  checked visual and test evidence in `docs/SUBMISSION_EVIDENCE.md`.

## Recording checklist

- 1920×1080, 30 fps or higher.
- Cursor movement deliberate; no terminal secrets, notifications, or unrelated tabs.
- Storefront and workspace pre-arranged side by side.
- One story only: false success → consent → regression.
- No claims of production adoption, harm prevented, or Shopify production support.
- Final video uploaded to a stable public host and linked from the README and Devpost.

The designed HTML shooting sheet remains available as `docs/demo-script.html`.
