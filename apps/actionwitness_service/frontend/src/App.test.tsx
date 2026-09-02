/**
 * §15.3's comparison fetch — the boundary cast this test exists to close.
 *
 * `GET /runs/{id}/comparison` used to be parsed with `value as
 * Record<string, unknown>` and its three list fields with `as readonly
 * string[] | undefined` — an unvalidated cast at the exact boundary the
 * constitution forbids one at. A server (or a proxy, or a future schema
 * change) that sent a truthy non-array for `differing_fields` would reach
 * `ComparisonPanel`'s `differingFields.join(", ")` and throw, and with no
 * error boundary below `<App />` in production that blanks the whole
 * workspace. This test drives that exact response through the real `App`
 * component and asserts the mismatch panel renders instead of crashing.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const WORKSPACE_BODY = {
  workspace_id: "ws_1",
  selected_target_id: "buggy-store",
  selected_contract_id: "con_1",
  scenario_mode: "pre_fix",
  failure_profile: null,
  active_run: {
    id: "run_1",
    status: "failed",
    target_id: "buggy-store",
    contract_id: "con_1",
    completed_at: "2026-01-01T00:00:00Z",
  },
  guidance: {
    phase: "failed",
    active_actor: "human_approver",
    next_actor: null,
    headline: "The run finished.",
    instruction: "Review the findings.",
    reason: "The run failed.",
    expected_consequence: "Nothing further happens automatically.",
    action_code: null,
    recovery_action_code: "reset_workspace",
    waiting_for: null,
    requires_human_input: false,
  },
  next_action: {
    actor: "human_approver",
    action_code: null,
    instruction: "Review the findings.",
    requires_human_input: false,
  },
  capabilities: {},
};

const RUN_BODY = {
  run_id: "run_1",
  status: "failed",
  overall_result: "failed",
  scenario_mode: "pre_fix",
  failure_profile: null,
  comparison_source_run_id: null,
  completed_at: "2026-01-01T00:00:00Z",
  pending_confirmation: null,
};

const FINDINGS_BODY = {
  run_id: "run_1",
  overall_result: "failed",
  findings: [],
  returned: 0,
  total: 0,
  failed: 0,
  elided: 0,
  report: "",
};

const EVENTS_BODY = {
  run_id: "run_1",
  run_status: "failed",
  events: [],
  next_after_sequence: 0,
  has_more: false,
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
}

function installFetch(comparisonBody: unknown, workspaceBody: unknown = WORKSPACE_BODY): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/contracts/templates")) {
        return jsonResponse({ templates: [] });
      }
      // Checked before `/runs/run_1`, because the regression-case route matches
      // both and the more specific one must win.
      if (url.includes("/eval")) {
        return jsonResponse({ cases: [] });
      }
      if (url.includes("/comparison")) {
        return jsonResponse(comparisonBody);
      }
      if (url.includes("/findings")) {
        return jsonResponse(FINDINGS_BODY);
      }
      if (url.includes("/events")) {
        return jsonResponse(EVENTS_BODY);
      }
      if (url.includes("/runs/run_1")) {
        return jsonResponse(RUN_BODY);
      }
      if (url.includes("/workspace")) {
        return jsonResponse(workspaceBody);
      }
      throw new Error(`unmocked fetch in test: ${url}`);
    }),
  );
}

describe("App — the comparison response is untrusted (§15.3, constitution §5)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the mismatch panel instead of crashing on a non-array differing_fields", async () => {
    installFetch({
      comparable: false,
      // A truthy non-array is exactly what `value as Record<string,
      // unknown>` plus an `as readonly string[] | undefined` cast let
      // through uncaught before this fix.
      differing_fields: "not-an-array",
      resolved_classifications: [],
      introduced_classifications: [],
    });

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText(/These runs cannot be compared/)).toBeDefined();
    });

    // The malformed field falls back to empty rather than reaching
    // `differingFields.join(", ")` with something that has no `.join`.
    expect(screen.getByText(/Differs in:/)).toBeDefined();
  });

  it("falls back to the null-comparison state when the whole body is not a record", async () => {
    installFetch("not-a-record-at-all");

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByText(/This run was not armed against a comparison source\./),
      ).toBeDefined();
    });
  });
});

describe("App — the lifecycle layout follows the reported phase (§11.5)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("highlights the stage the phase belongs to, and no other", async () => {
    installFetch({ comparable: null });

    const { container } = render(<App />);
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Findings" })).toBeDefined();
    });

    // The workspace reports `failed`, a verdict-stage phase. The highlight is
    // presentation over the server's word, never a decision of its own.
    expect(
      container.querySelector('section[data-stage="verdict"]')?.getAttribute("data-active"),
    ).toBe("true");
    expect(
      container.querySelector('section[data-stage="run"]')?.getAttribute("data-active"),
    ).toBeNull();
    // Exactly one stage says so in words — getByText throws on a second.
    expect(screen.getByText("current phase")).toBeDefined();
  });

  it("walks the reader from the banner to the control the server named", async () => {
    // The server names `review_findings`; the page maps that code to the
    // Findings panel and the shortcut lands focus there. The map decides
    // nothing — an unmapped code renders no button at all.
    installFetch(
      { comparable: null },
      {
        ...WORKSPACE_BODY,
        guidance: { ...WORKSPACE_BODY.guidance, action_code: "review_findings" },
      },
    );

    render(<App />);
    const go = await waitFor(() => screen.getByRole("button", { name: "Go to this step" }));

    fireEvent.click(go);

    expect(document.activeElement).toBe(screen.getByRole("region", { name: "Findings" }));
  });

  it("summarises the active run beside the title, from the same server facts", async () => {
    installFetch({ comparable: null });

    const { container } = render(<App />);
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Findings" })).toBeDefined();
    });

    const strip = container.querySelector(".workspace__status");
    expect(strip?.textContent).toContain("run_1 — failed");
    // The verdict, once findings landed — repeated display of the response the
    // Findings panel renders, not a second computation.
    await waitFor(() => {
      expect(strip?.textContent).toContain("Result:");
    });
  });
});

// --- §24: the regression surface has a human path ----------------------------

const CASE_ROW = {
  eval_case_id: "case_1",
  name: "one-mug-save20-no-checkout",
  content_hash: "sha256:abc",
  source_run_id: "run_1",
  schema_version: "1.0",
  created_at: "2026-01-01T00:00:00Z",
};

/** §15.4's collection route, matched exactly so a case route cannot shadow it. */
const LISTING = "/api/v1/evals";

/** The listing, each case's detail, and a replay — the three calls the panel makes. */
function installCaseFetch(latest: unknown, onReplay?: (body: string) => void): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url.includes("/contracts/templates")) {
        return jsonResponse({ templates: [] });
      }
      if (url.endsWith(LISTING)) {
        return jsonResponse({ cases: [CASE_ROW] });
      }
      if (url.includes("/case_1/runs")) {
        // Narrowed, not stringified: most of `BodyInit` stringifies to
        // "[object Object]", which would make the assertion on it vacuous.
        onReplay?.(typeof init?.body === "string" ? init.body : "");
        return jsonResponse({
          eval_run_id: "evrun_1",
          status: "passed",
          overall_result: "failed",
          environment: "reproduce_source",
        });
      }
      if (url.includes("/case_1")) {
        return jsonResponse({ ...CASE_ROW, latest_run: latest });
      }
      if (url.includes("/comparison")) {
        return jsonResponse({ comparable: null });
      }
      if (url.includes("/findings")) {
        return jsonResponse(FINDINGS_BODY);
      }
      if (url.includes("/events")) {
        return jsonResponse(EVENTS_BODY);
      }
      if (url.includes("/runs/run_1")) {
        return jsonResponse(RUN_BODY);
      }
      if (url.includes("/workspace")) {
        return jsonResponse(WORKSPACE_BODY);
      }
      throw new Error(`unmocked fetch in test: ${url}`);
    }),
  );
}

describe("App — regression cases are reachable without an agent (§24, AC-22)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the cases this workspace holds", async () => {
    // The panel and its own tests already existed; nothing rendered it, so the
    // regression surface had no human path and no browser test could reach it.
    installCaseFetch(null);

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Regression evals" })).toBeDefined();
    });
    expect(screen.getByText("one-mug-save20-no-checkout")).toBeDefined();
    expect(screen.getByText("sha256:abc")).toBeDefined();
  });

  it("shows the replay result and the target outcome as two separate facts", async () => {
    // §24.3: a reproduced failure is a *passing* replay whose target *failed*.
    // One merged number would report the product's best evidence as a broken
    // build, so both travel from the API to the screen unmerged.
    installCaseFetch({
      eval_run_id: "evrun_1",
      status: "passed",
      overall_result: "failed",
      environment: "reproduce_source",
    });

    render(<App />);

    const panel = await waitFor(() => screen.getByRole("region", { name: "Regression evals" }));
    expect(panel.textContent).toContain("Eval:");
    expect(panel.textContent).toContain("Target outcome:");
    expect(panel.textContent).toContain("reproduce_source");
  });

  it("replays a case against the environment the reader chose", async () => {
    const bodies: string[] = [];
    installCaseFetch(null, (body) => bodies.push(body));

    render(<App />);
    const panel = await waitFor(() => screen.getByRole("region", { name: "Regression evals" }));

    fireEvent.click(within(panel).getByRole("button", { name: "Replay against source" }));

    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    });
    expect(JSON.parse(bodies[0] ?? "{}")).toEqual({ environment: "reproduce_source" });

    // And the row updates from the replay's own response rather than from a
    // second read: the result is already in hand, and a re-read would be a
    // request the page's polling does not get to make.
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Regression evals" }).textContent).toContain(
        "Target outcome:",
      );
    });
  });

  it("offers to cut a case only from a run that failed", async () => {
    // FR-080: a passing run has no failure to reproduce, and the server refuses.
    // This workspace's run is `failed`, so the control is live.
    installCaseFetch(null);

    render(<App />);
    const panel = await waitFor(() => screen.getByRole("region", { name: "Regression evals" }));

    expect(
      within(panel)
        .getByRole("button", { name: /Create a regression/ })
        .hasAttribute("disabled"),
    ).toBe(false);
  });
});
