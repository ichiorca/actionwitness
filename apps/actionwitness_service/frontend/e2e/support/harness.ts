/**
 * Fixtures and page objects for the automated browser lane.
 *
 * Three things live here so no spec has to reinvent them:
 *
 * **A clean workspace.** `POST /workspace/reset {purge_completed: true}` is
 * FR-013's own recovery path: it cancels what is in flight, purges what
 * finished, reseeds the target, and keeps the selected contract. Running it
 * before every test keeps the suite order-independent inside one shared
 * workspace, and keeps `OUTCOME_RUNS_PER_WORKSPACE` from being the reason a
 * later test fails.
 *
 * **An agent.** `installWebMcpAgent` puts a conformant `document.modelContext`
 * on the page before the bundle boots, and `Agent` is the driver a spec uses to
 * behave like one — registering nothing itself, only reading `getTools()` and
 * invoking through the page's real handlers.
 *
 * **Assertions that wait rather than sleep.** Everything below is either a
 * web-first `expect` or an `expect.poll`. The suite contains no fixed delay: the
 * timeline polls once a second, registration settles on a React effect, and a
 * `waitForTimeout` tuned to either would be a flake waiting for a slower machine.
 */

import {
  type APIRequestContext,
  type APIResponse,
  type BrowserContextOptions,
  type Locator,
  type Page,
  expect,
  test as base,
} from "@playwright/test";

import { WORKSPACE_STATE_PATH } from "./globalSetup";
import { type AgentInvocation, installWebMcpAgent } from "./webmcpAgent";

export { expect };

/** §11.2's five target tools. */
export const SEARCH_CATALOG = "search_catalog";
export const GET_CART = "get_cart";
export const UPDATE_CART = "update_cart";
export const APPLY_DISCOUNT = "apply_discount";
export const PROCEED_TO_CHECKOUT = "proceed_to_checkout";

/** The §11.1 harness tools this UI registers. */
export const GET_WORKSPACE_STATUS = "get_workspace_status";
export const LIST_CONTRACT_TEMPLATES = "list_contract_templates";
export const ARM_OUTCOME_CONTRACT = "arm_outcome_contract";
export const VERIFY_OUTCOME = "verify_outcome";
export const GET_RUN_FINDINGS = "get_run_findings";
export const RESET_WORKSPACE = "reset_workspace";
export const CREATE_REGRESSION_EVAL = "create_regression_eval";

/** Seeded catalog identities (§13.1). Stable fixture metadata, not display text. */
export const MUG = "mug-ceramic-001";
export const NOTEBOOK = "notebook-001";
export const SAVE20 = "SAVE20";

/** Built-in contract templates, by the id the API reports as `source_template_id`. */
export const TEMPLATE_ONE_MUG_SAVE20 = "one_mug_save20_no_checkout";
export const TEMPLATE_RETRY_SAFE = "retry_safe_cart_update";
export const TEMPLATE_CONFIRMED_CHECKOUT = "confirmed_checkout_only";
export const TEMPLATE_NO_SIDE_EFFECTS = "one_mug_no_side_effects";
export const TEMPLATE_STABLE_SURFACE = "one_mug_stable_surface";

const TERMINAL_PHASES = ["passed", "passed_with_warnings", "failed", "error", "cancelled"];

/** A tool result as both registration paths normalize it (§11.4). */
export interface ToolResult {
  readonly content: readonly { readonly type: string; readonly text: string }[];
  readonly isError?: boolean;
}

/** The first text block of a tool result, parsed as the JSON body the route returned. */
export function bodyOf(result: ToolResult): Record<string, unknown> {
  const text = result.content[0]?.text ?? "";
  return JSON.parse(text) as Record<string, unknown>;
}

/** The first text block, unparsed — for error results, which carry a message. */
export function textOf(result: ToolResult): string {
  return result.content[0]?.text ?? "";
}

/**
 * An agent driving the page's registered tools.
 *
 * Deliberately thin. Everything it can do, a real agent can do; nothing it does
 * reaches into React state or the application's own modules.
 */
export class Agent {
  constructor(private readonly page: Page) {}

  async toolNames(): Promise<string[]> {
    return await this.page.evaluate(() => window.__awAgent?.toolNames() ?? []);
  }

  async describe(): Promise<{ name: string; description: string; inputSchema: unknown }[]> {
    return await this.page.evaluate(() => window.__awAgent?.describe() ?? []);
  }

  async toolChangeCount(): Promise<number> {
    return await this.page.evaluate(() => window.__awAgent?.toolChangeCount() ?? 0);
  }

  /** Wait until the browser registry reports this tool. Registration is an effect. */
  async expectRegistered(name: string): Promise<void> {
    await expect
      .poll(async () => await this.toolNames(), {
        message: `waiting for ${name} to appear in getTools()`,
      })
      .toContain(name);
  }

  async expectNotRegistered(name: string): Promise<void> {
    await expect
      .poll(async () => await this.toolNames(), {
        message: `waiting for ${name} to leave getTools()`,
      })
      .not.toContain(name);
  }

  /** Invoke the way the pinned build does — arguments only, no context (ADR-0002). */
  async invoke(name: string, args: Record<string, unknown> = {}): Promise<ToolResult> {
    await this.expectRegistered(name);
    return (await this.page.evaluate(
      async ([toolName, toolArgs]) =>
        await window.__awAgent?.invokePinned(toolName as string, toolArgs as Record<string, unknown>),
      [name, args] as const,
    )) as ToolResult;
  }

  /** Invoke and assert it succeeded, returning the parsed body. */
  async call(name: string, args: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    const result = await this.invoke(name, args);
    expect(result.isError, `${name} returned an error result: ${textOf(result)}`).toBeFalsy();
    return bodyOf(result);
  }

  /** Begin an invocation that carries an abortable signal, without awaiting it. */
  async start(handle: string, name: string, args: Record<string, unknown> = {}): Promise<void> {
    await this.expectRegistered(name);
    await this.page.evaluate(
      ([h, n, a]) => {
        window.__awAgent?.start(h as string, n as string, a as Record<string, unknown>);
      },
      [handle, name, args] as const,
    );
  }

  async abort(handle: string): Promise<void> {
    await this.page.evaluate((h) => {
      window.__awAgent?.abort(h);
    }, handle);
  }

  async poll(handle: string): Promise<AgentInvocation> {
    return (await this.page.evaluate(
      (h) => window.__awAgent?.poll(h),
      handle,
    )) as AgentInvocation;
  }

  /** Wait for a started invocation to settle, then return how it settled. */
  async settled(handle: string): Promise<AgentInvocation> {
    await expect
      .poll(async () => (await this.poll(handle)).state, {
        message: `waiting for invocation ${handle} to settle`,
        timeout: 30_000,
      })
      .not.toBe("pending");
    return await this.poll(handle);
  }

  async isPending(handle: string): Promise<boolean> {
    return (await this.poll(handle)).state === "pending";
  }

  /** Register a look-alike the way a third-party script on the origin would. */
  async injectTool(
    name: string,
    description: string,
    inputSchema: Record<string, unknown> = { type: "object", properties: {} },
  ): Promise<void> {
    await this.page.evaluate(
      async ([n, d, s]) => {
        await window.__awAgent?.injectTool(
          n as string,
          d as string,
          s as Record<string, unknown>,
        );
      },
      [name, description, inputSchema] as const,
    );
  }
}

/** The workspace page, addressed the way a person reads it. */
export class Workspace {
  constructor(readonly page: Page) {}

  /**
   * Load the workspace, honouring the rate limiter on the document itself.
   *
   * FR-009 exempts health and static assets from the request bucket, and `/` is
   * neither: the index document is served by a route, so a burst of navigations
   * can be answered with the error envelope instead of the bundle. That is the
   * product behaving as specified, so the lane waits the interval the server
   * named rather than pretending the limit is not there.
   */
  async open(): Promise<void> {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const response = await this.page.goto("/");
      if (response?.status() !== 429) {
        break;
      }
      const retryAfter = Number(response.headers()["retry-after"] ?? "1");
      await this.page.waitForTimeout((Number.isFinite(retryAfter) ? retryAfter : 1) * 1000);
    }
    await expect(this.page.getByRole("heading", { name: "ActionWitness", level: 1 })).toBeVisible();
    // The banner is server-derived; its presence means the first `GET /workspace`
    // landed, which every later assertion depends on.
    await expect(this.banner).toBeVisible();
  }

  /**
   * Continue as a later session by the same operator.
   *
   * A test that drives two complete journeys — a failing `pre_fix` run and the
   * `post_fix` rerun it is compared against — is modelling two sittings, not one
   * burst. Presenting the second as its own client is the faithful version of
   * that, and it keeps the first journey's polling from spending the second's
   * request allowance. The cookie jar is untouched, so it is the same workspace
   * throughout, which is the whole point of the pair.
   */
  async newSession(): Promise<void> {
    await this.page.context().setExtraHTTPHeaders({ [CLIENT_HEADER]: nextClientAddress() });
  }

  get banner(): Locator {
    return this.page.locator("section.banner");
  }

  /** The server's compact action code, rendered hidden for exactly this purpose. */
  get actionCode(): Locator {
    return this.page.getByTestId("banner-action-code");
  }

  get phase(): Locator {
    return this.page.locator("section.banner[data-phase]");
  }

  /**
   * One panel, by its `aria-label`.
   *
   * `exact` matters: "Contract" and "Create a contract" are two panels, and a
   * substring match resolves to both — which fails as a strict-mode violation
   * rather than as the wrong assertion, but only after the reader has gone
   * looking for a rendering bug.
   */
  panel(label: string): Locator {
    return this.page.getByRole("region", { name: label, exact: true });
  }

  get capabilities(): Locator {
    return this.page.getByRole("region", { name: "Capabilities" });
  }

  get findings(): Locator {
    return this.panel("Findings");
  }

  get timeline(): Locator {
    return this.panel("Agent activity");
  }

  get dialog(): Locator {
    return this.page.getByRole("dialog");
  }

  get alerts(): Locator {
    return this.page.getByRole("alert");
  }

  async expectPhase(phase: string): Promise<void> {
    await expect(this.phase).toHaveAttribute("data-phase", phase);
  }

  async expectActionCode(code: string): Promise<void> {
    await expect(this.actionCode).toHaveText(code);
  }

  async expectTerminalPhase(): Promise<void> {
    await expect
      .poll(async () => await this.phase.getAttribute("data-phase"), {
        message: "waiting for the run to reach a terminal phase",
        timeout: 40_000,
      })
      .toMatch(new RegExp(`^(${TERMINAL_PHASES.join("|")})$`));
  }

  /**
   * Select a built-in contract through the UI.
   *
   * The panel labels each button with its `source_template_id` and marks the
   * chosen one `aria-pressed`, so both halves of "it was selected" are read
   * from what a screen reader would announce rather than from a class name.
   */
  async selectTemplate(templateId: string): Promise<void> {
    const button = this.panel("Contract").getByRole("button", { name: templateId, exact: true });
    await button.click();
    await expect(button).toHaveAttribute("aria-pressed", "true");
  }

  async setScenarioMode(mode: "pre_fix" | "post_fix"): Promise<void> {
    await this.panel("Configuration").getByRole("radio", { name: mode, exact: true }).check();
  }

  /** §13.3's profile field commits on blur, so the blur is part of the action. */
  async setFailureProfile(profile: string): Promise<void> {
    const field = this.panel("Configuration").getByRole("textbox", { name: /profile/i });
    await field.fill(profile);
    await field.blur();
  }

  /**
   * Arm the selected contract, and wait until it is armed.
   *
   * The wait is part of the action, not politeness. A helper that returned on
   * the click would leave every caller to remember that `POST /runs` is still
   * in flight — and the ones that forgot would read `active_run` as `null` and
   * fail somewhere unrelated to the thing they were testing.
   */
  async arm(): Promise<void> {
    await this.panel("Target").getByRole("button", { name: /arm/i }).click();
    await this.expectPhase("armed");
  }

  /** Verify the outcome, and wait for the verdict. */
  async verify(): Promise<void> {
    await this.panel("Target").getByRole("button", { name: /verify/i }).click();
    await this.expectTerminalPhase();
  }

  async reset(): Promise<void> {
    await this.panel("Configuration").getByRole("button", { name: /reset/i }).click();
  }
}

/**
 * Direct API access carrying the same workspace cookie the page holds.
 *
 * Used for arrangement and for reading evidence the UI paginates, never to
 * assert something the browser was supposed to show — that would test the
 * server twice and the page not at all.
 */
export class HarnessApi {
  /**
   * The template listing, cached for the process.
   *
   * §15.2's built-ins are seeded once at startup and never change, so re-reading
   * them per test would spend the request budget below on a constant.
   */
  private static templateCache: { contract_id: string; source_template_id: string }[] | null = null;

  constructor(private readonly api: APIRequestContext) {}

  /**
   * One request, paced by FR-009's own answer.
   *
   * The limiter allows 120 requests a minute with a burst of 30, and this suite
   * — one browser plus one API client, both on loopback — can outrun that in a
   * few seconds. The limits are *not* raised for the lane: weakening a rail to
   * make tests convenient is the trade the constitution names outright. Instead
   * this does what any correct client does with a 429 and waits the interval the
   * server named in `Retry-After`, which paces the whole suite to the documented
   * rate without a single arbitrary sleep.
   *
   * Bounded at three attempts. A limiter that is still refusing after three
   * honoured intervals is a finding, not something to keep retrying past.
   */
  private async send(
    method: "get" | "post" | "put" | "delete",
    path: string,
    data?: unknown,
  ): Promise<APIResponse> {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const response = await (data === undefined
        ? this.api[method](path)
        : this.api[method](path, { data }));
      if (response.status() !== 429) {
        return response;
      }
      const retryAfter = Number(response.headers()["retry-after"] ?? "1");
      await new Promise((resolve) =>
        setTimeout(resolve, (Number.isFinite(retryAfter) ? retryAfter : 1) * 1000),
      );
    }
    throw new Error(`${method.toUpperCase()} ${path} stayed rate-limited across three intervals`);
  }

  private async ok(
    method: "get" | "post" | "put" | "delete",
    path: string,
    data?: unknown,
  ): Promise<APIResponse> {
    const response = await this.send(method, path, data);
    expect(response.ok(), `${method.toUpperCase()} ${path}: ${await response.text()}`).toBeTruthy();
    return response;
  }

  async workspace(): Promise<Record<string, unknown>> {
    return (await (await this.ok("get", "/api/v1/workspace")).json()) as Record<string, unknown>;
  }

  async reset(purge = true): Promise<void> {
    await this.ok("post", "/api/v1/workspace/reset", { purge_completed: purge });
  }

  async setScenarioMode(mode: string): Promise<void> {
    await this.ok("put", "/api/v1/workspace/scenario-mode", { scenario_mode: mode });
  }

  async setFailureProfile(profile: string | null): Promise<void> {
    await this.ok("put", "/api/v1/workspace/failure-profile", { failure_profile: profile });
  }

  async templates(): Promise<{ contract_id: string; source_template_id: string }[]> {
    if (HarnessApi.templateCache === null) {
      const body = (await (await this.ok("get", "/api/v1/contracts/templates")).json()) as {
        templates?: { contract_id: string; source_template_id: string }[];
      };
      HarnessApi.templateCache = body.templates ?? [];
    }
    return HarnessApi.templateCache;
  }

  /** Select the built-in contract expanded from `templateId`. */
  async selectTemplate(templateId: string): Promise<string> {
    const match = (await this.templates()).find(
      (template) => template.source_template_id === templateId,
    );
    expect(match, `no built-in template named ${templateId}`).toBeDefined();
    const contractId = match?.contract_id ?? "";
    await this.ok("post", `/api/v1/contracts/${contractId}/select`);
    return contractId;
  }

  /** §11.4 caps this page at ten findings; the untruncated total comes with it. */
  async findings(runId: string, limit = 10): Promise<Record<string, unknown>> {
    const response = await this.ok("get", `/api/v1/runs/${runId}/findings?limit=${String(limit)}`);
    return (await response.json()) as Record<string, unknown>;
  }

  async run(runId: string): Promise<Record<string, unknown>> {
    return (await (await this.ok("get", `/api/v1/runs/${runId}`)).json()) as Record<string, unknown>;
  }

  async events(runId: string, limit = 100): Promise<Record<string, unknown>> {
    const response = await this.ok(
      "get",
      `/api/v1/runs/${runId}/events?after_sequence=0&limit=${String(limit)}`,
    );
    return (await response.json()) as Record<string, unknown>;
  }

  async report(runId: string): Promise<Record<string, unknown>> {
    return (await (await this.ok("get", `/api/v1/runs/${runId}/report`)).json()) as Record<
      string,
      unknown
    >;
  }

  /** The raw client, for tests that assert on a refusal rather than a success. */
  get raw(): APIRequestContext {
    return this.api;
  }
}

/** The header §20.1's trusted-proxy rule reads the client identity from. */
export const CLIENT_HEADER = "X-Forwarded-For";

/**
 * An explicitly empty cookie jar, for a visitor who has never seen the harness.
 *
 * `browser.newContext()` called inside a test inherits the project's
 * `use.storageState` — which is the suite's shared workspace cookie. A test that
 * omitted this would hand its "fresh" visitor an existing workspace and then
 * assert isolation against itself, which passes and proves nothing.
 */
export function emptyStorage(): NonNullable<BrowserContextOptions["storageState"]> {
  return { cookies: [], origins: [] };
}

/**
 * Distinct simulated clients, so the suite is not one client.
 *
 * FR-009's buckets are keyed per client — 120 requests a minute, burst 30 — and
 * the whole suite would otherwise share one: a browser polling a run once a
 * second, plus a fixture doing four setup calls, outruns that within a few
 * tests, and the page starts rendering the 429 envelope instead of the
 * workspace.
 *
 * The limits are **not** raised. Instead the lane runs the service the way it is
 * deployed — behind a trusted loopback proxy — and each simulated client gets
 * exactly the allowance one real user gets. That also exercises `client_key`'s
 * trusted-proxy branch, which nothing else covers end to end: a forwarding
 * header is honoured only when the direct peer is configured as a proxy, and
 * this is the only place that is true.
 *
 * The browser and the arranging API client are two addresses rather than one,
 * because they are two clients: in real use the page is a browser and the
 * arrangement is a CLI. Keeping them apart also stops setup traffic from
 * spending the page's allowance, which is what the page needs for its polling.
 *
 * Documentation ranges (RFC 5737), so nothing here reads as a real network. A
 * plain counter is safe because the lane is serial by configuration.
 */
let clientCounter = 0;
function nextClientAddress(prefix = "203.0.113"): string {
  clientCounter += 1;
  return `${prefix}.${String(clientCounter % 250)}`;
}

interface HarnessFixtures {
  /** Auto-used: a purged, reseeded workspace in its documented default state. */
  cleanWorkspace: void;
  /** The simulated client address this test's browser and API calls present. */
  clientAddress: string;
  harness: HarnessApi;
  workspace: Workspace;
  agent: Agent;
}

export const test = base.extend<HarnessFixtures>({
  // Playwright reads a fixture's dependencies from its destructuring pattern
  // and rejects any other parameter shape, so a fixture that depends on nothing
  // must still destructure nothing.
  // eslint-disable-next-line no-empty-pattern
  clientAddress: async ({}, use) => {
    await use(nextClientAddress());
  },

  // Every request the page makes — navigation, workspace reads, tool
  // invocations, timeline polls — carries this test's client identity.
  context: async ({ context, clientAddress }, use) => {
    await context.setExtraHTTPHeaders({ [CLIENT_HEADER]: clientAddress });
    await use(context);
  },

  harness: async ({ playwright, baseURL }, use) => {
    // A context of its own rather than the built-in `request` fixture, so the
    // header travels with the arrangement calls too. It shares the cookie jar
    // through `storageState`, which is what keeps it in the same workspace as
    // the page, and it presents as its own client for the reason above.
    const api = await playwright.request.newContext({
      ...(baseURL === undefined ? {} : { baseURL }),
      storageState: WORKSPACE_STATE_PATH,
      extraHTTPHeaders: { [CLIENT_HEADER]: nextClientAddress("198.51.100") },
    });
    await use(new HarnessApi(api));
    await api.dispose();
  },

  cleanWorkspace: [
    async ({ harness }, use) => {
      // Before, not after: a test that fails mid-run should leave its evidence
      // in place for the trace, and the next test is the one that needs a clean
      // slate.
      await harness.reset(true);
      // A contract, before the scenario controls. FR-013 deliberately *retains*
      // the selected contract across a reset, and the target is selected as a
      // side effect of selecting a contract — so scenario mode is refused with
      // "no target is selected" until one has been chosen at least once. Every
      // test either uses this default or selects its own over the top.
      await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
      // The documented starting selection: the canonical pre-fix scenario with
      // no fault injected. A test asking for a fault says so itself, so a
      // profile can never leak from the test that ran before it.
      await harness.setScenarioMode("pre_fix");
      await harness.setFailureProfile(null);
      await use();
    },
    { auto: true },
  ],

  workspace: async ({ page }, use) => {
    await use(new Workspace(page));
  },

  agent: async ({ page }, use) => {
    // Before any navigation, so the bundle finds the registry already present —
    // the same ordering a browser with native WebMCP gives it.
    await page.addInitScript(installWebMcpAgent);
    await use(new Agent(page));
  },
});
