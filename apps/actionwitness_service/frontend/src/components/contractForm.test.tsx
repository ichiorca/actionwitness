/**
 * 012-T5 — the declarative contract form (§25.2, FR-021, AC-02).
 *
 * AC-02 asks that "the expected native, imperative, and declarative tools are
 * visible with valid schemas" when the page is inspected in Chrome DevTools.
 * jsdom has no WebMCP implementation, so *no* test here can prove the browser
 * registered anything — a declarative tool exists because the browser parsed
 * the markup, and there is no call to intercept. `tests/browser/` carries the
 * operator checklist for that half.
 *
 * What is testable, and what these tests cover, is everything the browser will
 * read and everything the page does with what it sends back: the annotations
 * §25.2 requires, `preventDefault`, one submit path shared by both callers,
 * `respondWith` for the agent's promise, `toolactivated`/`toolcancel`
 * visibility, and the allowlist the server enforces being reflected rather
 * than reinvented.
 *
 * The one worth reading twice is
 * `an agent submission and a human submission send the same payload`. §25.2
 * requires the declarative path to "post the same payload to FastAPI used by a
 * human submission" — if those ever diverged, an agent could reach a request
 * shape no person could produce, and the human UI would stop being a faithful
 * preview of what the agent can do.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { parseTemplates } from "../api/contracts";
import { ContractForm } from "./ContractForm";

const TEMPLATES = parseTemplates({
  templates: [
    {
      contract_id: "tpl_a",
      source_template_id: "one_mug_save20_no_checkout",
      name: "one-mug-save20-no-checkout",
      description: "Add one mug, apply SAVE20, and do not create an order.",
      target_id: "buggy-store",
      schema_version: "1.0",
      content_hash: "sha256:aaa",
      parameters: ["quantity", "discount_code"],
    },
    {
      contract_id: "tpl_b",
      source_template_id: "confirmed_checkout_only",
      name: "confirmed-checkout-only",
      description: "Create one order, and only behind an approved confirmation.",
      target_id: "buggy-store",
      schema_version: "1.0",
      content_hash: "sha256:bbb",
      parameters: [],
    },
  ],
});

const CREATED = {
  contract_id: "ctr_1",
  name: "one-mug-save20-no-checkout",
  content_hash: "sha256:ccc",
  source_template_id: "one_mug_save20_no_checkout",
  schema_version: "1.0",
  document: {},
};

let fetchMock: ReturnType<typeof vi.fn>;

function respondWith(body: unknown, status = 200): void {
  fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
}

function renderForm(onCreated = vi.fn()): { onCreated: ReturnType<typeof vi.fn> } {
  render(<ContractForm templates={TEMPLATES} onCreated={onCreated} />);
  return { onCreated };
}

function form(): HTMLFormElement {
  const element = document.querySelector("form");
  if (element === null) {
    throw new Error("the contract form did not render");
  }
  return element;
}

/**
 * Fire a submission the way an agent does, and collect what it is answered with.
 *
 * The promise is collected into an array rather than assigned to a `let`:
 * TypeScript's control-flow analysis cannot see an assignment made inside a
 * callback, so a `let` initialised to `null` stays `null` as far as the
 * compiler is concerned.
 */
function agentSubmit(): Array<Promise<unknown>> {
  const answers: Array<Promise<unknown>> = [];
  const event = new Event("submit", { bubbles: true, cancelable: true });
  Object.assign(event, {
    agentInvoked: true,
    respondWith: (result: Promise<unknown>) => {
      answers.push(result);
    },
  });
  fireEvent(form(), event);
  return answers;
}

async function answered(answers: Array<Promise<unknown>>): Promise<unknown> {
  const first = answers[0];
  if (first === undefined) {
    throw new Error("the agent's submission was never answered");
  }
  return first;
}

/** The body of the single request the form made. */
function sentBody(): unknown {
  const call = fetchMock.mock.calls[0];
  if (call === undefined) {
    throw new Error("no request was made");
  }
  const init = call[1] as RequestInit;
  if (typeof init.body !== "string") {
    throw new Error("the request carried no JSON body");
  }
  return JSON.parse(init.body) as unknown;
}

beforeEach(() => {
  respondWith(CREATED, 201);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// --- §25.2's annotations ------------------------------------------------------

describe("what the browser reads off the markup", () => {
  it("declares the tool on the form itself", () => {
    // Arrange / Act
    renderForm();

    // Assert — this pair *is* the registration. Without them the form is a form.
    expect(form().getAttribute("toolname")).toBe("create_outcome_contract");
    expect(form().getAttribute("tooldescription")).toContain("trusted built-in template");
  });

  it("describes every control an agent may fill in", () => {
    // Arrange / Act
    renderForm();

    // Assert — a control with no description is a parameter with no meaning,
    // and an agent guessing at one is how a form gets filled in wrongly.
    for (const name of ["template_id", "contract_name", "quantity", "discount_code"]) {
      const control = form().querySelector(`[name="${name}"]`);
      expect(control?.getAttribute("toolparamdescription")).toBeTruthy();
    }
  });

  it("marks exactly one control as the one an agent may submit", () => {
    // Arrange / Act
    renderForm();

    // Assert
    const submittable = form().querySelectorAll("[toolautosubmit]");
    expect(submittable).toHaveLength(1);
    expect(submittable[0]?.getAttribute("type")).toBe("submit");
  });

  it("says the tool cannot author assertions", () => {
    // Arrange / Act
    renderForm();

    // Assert — the description is the only thing an agent reads before
    // choosing this tool, so the limit belongs in it rather than only in a
    // rejection it discovers afterwards.
    expect(form().getAttribute("tooldescription")).toContain("cannot author arbitrary assertions");
  });
});

// --- submission ---------------------------------------------------------------

describe("submitting", () => {
  it("never navigates", async () => {
    // Arrange
    renderForm();
    const event = new Event("submit", { bubbles: true, cancelable: true });

    // Act
    fireEvent(form(), event);

    // Assert — a declarative form that navigated would tear down the page the
    // agent is mid-conversation with.
    expect(event.defaultPrevented).toBe(true);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
  });

  it("posts the flat scalars to the contracts endpoint", async () => {
    // Arrange
    renderForm();
    fireEvent.change(screen.getByLabelText("Name (optional)"), {
      target: { value: "Rehearsal" },
    });
    fireEvent.change(screen.getByLabelText("Quantity"), { target: { value: "3" } });

    // Act
    fireEvent.submit(form());

    // Assert
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/v1/contracts", expect.anything());
    });
    expect(sentBody()).toEqual({
      template_id: "one_mug_save20_no_checkout",
      contract_name: "Rehearsal",
      quantity: "3",
    });
  });

  it("omits a control the person left alone", async () => {
    // Arrange — absence means "use the template's own value". An empty string
    // would be a value the template must then reject, which turns leaving a
    // field blank into an error.
    renderForm();

    // Act
    fireEvent.submit(form());

    // Assert
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    expect(sentBody()).toEqual({ template_id: "one_mug_save20_no_checkout" });
  });

  it("hands the created contract to the workspace", async () => {
    // Arrange
    const { onCreated } = renderForm();

    // Act
    fireEvent.submit(form());

    // Assert
    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledWith(
        expect.objectContaining({ contractId: "ctr_1", contentHash: "sha256:ccc" }),
      );
    });
  });
});

// --- the agent's half ---------------------------------------------------------

describe("an agent submission", () => {
  it("resolves the agent's call with the created contract", async () => {
    // Arrange — `respondWith` is how the agent learns the request finished.
    // Without it the call resolves the instant the handler returns, which is
    // before the contract exists.
    renderForm();
    const answers = agentSubmit();

    // Assert
    await waitFor(() => {
      expect(answers).toHaveLength(1);
    });
    const result = (await answered(answers)) as { content: Array<{ text: string }> };
    expect(result.content[0]?.text).toContain("ctr_1");
  });

  it("reports a refusal as an error rather than a result", async () => {
    // Arrange — a tool that returned success for a refused submission would be
    // the false self-report this whole product exists to catch.
    respondWith(
      {
        error: {
          code: "CONTRACT_VALIDATION_FAILED",
          message: "The template could not be instantiated from those values.",
          retryable: false,
          details: [{ path: "quantity", message: "quantity must be between 1 and 5" }],
        },
      },
      422,
    );
    renderForm();
    const answers = agentSubmit();

    // Assert
    await waitFor(() => {
      expect(answers).toHaveLength(1);
    });
    const result = (await answered(answers)) as { isError?: boolean };
    expect(result.isError).toBe(true);
  });

  it("sends the same payload a person would", async () => {
    // Arrange — §25.2's "the same payload to FastAPI used by a human
    // submission". One handler serves both, so this pins that they were not
    // allowed to drift into two.
    renderForm();
    fireEvent.change(screen.getByLabelText("Quantity"), { target: { value: "2" } });
    fireEvent.submit(form());
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    const human = sentBody();

    respondWith(CREATED, 201);
    const agentEvent = new Event("submit", { bubbles: true, cancelable: true });
    Object.assign(agentEvent, { agentInvoked: true, respondWith: () => undefined });

    // Act
    fireEvent(form(), agentEvent);

    // Assert
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    expect(sentBody()).toEqual(human);
  });
});

// --- agent presence -----------------------------------------------------------

describe("agent activity", () => {
  it("says when an agent has taken the form", () => {
    // Arrange
    renderForm();

    // Act
    fireEvent(form(), new Event("toolactivated", { bubbles: true }));

    // Assert — a form quietly filled in by something the person cannot see is
    // the failure mode this product exists to make visible.
    expect(screen.getByText("An agent is filling in this form.")).toBeTruthy();
  });

  it("says when an agent gave up, and that nothing was created", () => {
    // Arrange
    renderForm();

    // Act
    fireEvent(form(), new Event("toolcancel", { bubbles: true }));

    // Assert — "cancelled" and "failed" look alike from the outside; the
    // difference is whether anything happened, so it is said outright.
    expect(screen.getByText(/cancelled its submission/)).toBeTruthy();
  });
});

// --- the allowlist ------------------------------------------------------------

describe("the per-template allowlist", () => {
  it("disables the controls a template does not accept", () => {
    // Arrange
    renderForm();

    // Act — `confirmed_checkout_only` says nothing about quantity.
    fireEvent.change(screen.getByLabelText("Template"), {
      target: { value: "confirmed_checkout_only" },
    });

    // Assert
    // `disabled` read off the element: this project has no jest-dom, and the
    // property is what actually keeps the value out of `FormData`.
    expect(form().querySelector<HTMLInputElement>('[name="quantity"]')?.disabled).toBe(true);
    expect(form().querySelector<HTMLSelectElement>('[name="discount_code"]')?.disabled).toBe(
      true,
    );
    expect(screen.getByText("This template does not use a quantity.")).toBeTruthy();
  });

  it("keeps a disabled control out of the submission entirely", async () => {
    // Arrange — the server rejects an unallowlisted scalar rather than
    // ignoring it, so sending one the person could not clear would refuse a
    // submission they had no way to fix.
    renderForm();
    fireEvent.change(screen.getByLabelText("Quantity"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Template"), {
      target: { value: "confirmed_checkout_only" },
    });

    // Act
    fireEvent.submit(form());

    // Assert
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    expect(sentBody()).toEqual({ template_id: "confirmed_checkout_only" });
  });

  it("does not remove the control, so the tool's shape stays put", () => {
    // Arrange / Act
    renderForm();
    fireEvent.change(screen.getByLabelText("Template"), {
      target: { value: "confirmed_checkout_only" },
    });

    // Assert — an agent that read this form once must not find a parameter
    // gone because a person changed a dropdown.
    expect(form().querySelector('[name="quantity"]')).not.toBeNull();
    expect(
      form().querySelector('[name="quantity"]')?.getAttribute("toolparamdescription"),
    ).toBeTruthy();
  });
});

// --- refusals -----------------------------------------------------------------

describe("a refused submission", () => {
  it("names the control the server rejected", async () => {
    // Arrange
    respondWith(
      {
        error: {
          code: "CONTRACT_VALIDATION_FAILED",
          message: "The template could not be instantiated from those values.",
          retryable: false,
          details: [{ path: "quantity", message: "quantity must be between 1 and 5" }],
        },
      },
      422,
    );
    renderForm();

    // Act
    fireEvent.submit(form());

    // Assert — §15.8's details exist so a person can see which control to fix
    // rather than being told the submission was invalid and left to guess.
    await waitFor(() => {
      expect(screen.getByText("quantity must be between 1 and 5")).toBeTruthy();
    });
  });

  it("resolves a body-shape rejection to the same control", async () => {
    // Arrange — the expansion says `quantity`, FastAPI's own validation says
    // `body.quantity`. Two boundaries, one envelope; the person should see one
    // message beside one field either way.
    respondWith(
      {
        error: {
          code: "CONTRACT_VALIDATION_FAILED",
          message: "The request was not in an acceptable shape.",
          retryable: false,
          details: [{ path: "body.quantity", message: "Input should be a valid integer" }],
        },
      },
      422,
    );
    renderForm();

    // Act
    fireEvent.submit(form());

    // Assert
    await waitFor(() => {
      expect(screen.getByText("Input should be a valid integer")).toBeTruthy();
    });
  });

  it("creates nothing when the server refuses", async () => {
    // Arrange
    respondWith(
      {
        error: {
          code: "CONTRACT_VALIDATION_FAILED",
          message: "no",
          retryable: false,
          details: [],
        },
      },
      422,
    );
    const { onCreated } = renderForm();

    // Act
    fireEvent.submit(form());

    // Assert
    await waitFor(() => {
      expect(screen.getByText("no")).toBeTruthy();
    });
    expect(onCreated).not.toHaveBeenCalled();
  });
});
