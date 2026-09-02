/**
 * The local WebMCP lifecycle adapter — the ONLY module application code may
 * import for tool registration (spec v1.9 §11, §25.1; constitution §1).
 *
 * Everything else in this app talks to `document.modelContext` through here, so
 * the rest of the UI is testable without a browser that has WebMCP at all, and
 * so there is exactly one place to audit when the browser API changes.
 *
 * ## Three registration paths, and why each exists
 *
 * §25's three mechanisms, and AC-02 expects all three to be visible in a
 * browser at once. Two of them are a choice forced by a package pin; the third
 * is a different kind of registration entirely.
 *
 * ADR-0002 pinned `use-webmcp-tool@0.2.0`. Its `execute` signature is
 * `(args) => Result` — **it forwards no per-invocation `AbortSignal`**. That is
 * not a defect in the package; it is the exact gap ADR-0002's "rule 3 split"
 * anticipated, and it decides which of the first two paths each tool uses:
 *
 * - `useHarnessTool` wraps the pinned hook. Correct for tools whose work is a
 *   single request the browser can abandon harmlessly.
 * - `useNativeTool` registers directly and hands the handler its invocation
 *   signal. Required for `get_workspace_status` (§11.1 specifies native) and
 *   for anything cancellation-sensitive — `proceed_to_checkout` waits on a
 *   human, and an agent that abandons the call must be able to cancel the
 *   confirmation rather than leave it pending (FR-037, §14.9).
 * - `useDeclarativeTool` registers nothing. The browser reads `toolname` off a
 *   visible `<form>` and the tool exists because the markup does (§25.2). Used
 *   for `create_outcome_contract`, where the agent's affordance and the
 *   person's affordance are deliberately the same DOM node.
 *
 * ## Lifecycle
 *
 * Registration is undone by aborting the `AbortSignal` given to
 * `registerTool`, so cleanup is a property of the registration rather than a
 * separate call that can be missed. React StrictMode deliberately mounts,
 * unmounts and remounts effects: a correct adapter therefore calls
 * `registerTool` twice and leaves exactly one live tool. Both halves are
 * asserted — counting only survivors would hide a leak, counting only calls
 * would report a false one.
 *
 * ## Results
 *
 * Every result leaves here as `{ content: [{ type: "text", text }], isError? }`
 * within §11.4's character budget. A thrown handler becomes `isError: true`
 * rather than a rejected promise, because a rejection reaches an agent as a
 * transport failure and tells it nothing about what to do next.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, MutableRefObject } from "react";
import { useWebMCP } from "use-webmcp-tool";

export const MAX_TOOL_RESULT_CHARS = 1_500;
/** §11.4's one normative exception, for `get_run_findings`. */
export const MAX_FINDINGS_RESULT_CHARS = 4_000;

const TRUNCATION_MARKER = "…[truncated]";

export interface NormalizedToolResult {
  readonly content: ReadonlyArray<{ readonly type: "text"; readonly text: string }>;
  readonly isError?: boolean;
}

export type RegistrationPhase = "unsupported" | "registering" | "registered" | "failed";

export interface RegistrationState {
  readonly phase: RegistrationPhase;
  readonly detail: string;
}

const UNSUPPORTED: RegistrationState = {
  phase: "unsupported",
  detail: "This browser has no WebMCP. The full workspace remains usable without it.",
};

/**
 * Feature detection. Type availability never proves browser support (§25.12),
 * so this asks the document rather than the user agent.
 */
export function isWebMcpSupported(): boolean {
  return typeof document !== "undefined" && document.modelContext !== undefined;
}

/**
 * Wrap a value as a bounded text result.
 *
 * Truncation is marked. A silently clipped result is worse than a short one: a
 * reader cannot tell a complete answer from half of one.
 */
export function normalizeResult(
  value: unknown,
  limit: number = MAX_TOOL_RESULT_CHARS,
): NormalizedToolResult {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? null);
  const bounded =
    text.length <= limit ? text : text.slice(0, limit - TRUNCATION_MARKER.length) + TRUNCATION_MARKER;
  return { content: [{ type: "text", text: bounded }] };
}

/**
 * Wrap a failure as `isError: true` (§11.4).
 *
 * Never the raw error. §20 keeps internals out of anything an agent reads, and
 * a stack trace is both a leak and useless to the caller — so the message is
 * whatever the throwing code chose to say, and nothing more.
 */
export function normalizeError(
  error: unknown,
  limit: number = MAX_TOOL_RESULT_CHARS,
): NormalizedToolResult {
  const message =
    error instanceof Error && error.message !== "" ? error.message : "The tool call failed.";
  return { ...normalizeResult(message, limit), isError: true };
}

export interface HarnessToolDefinition<Args = Record<string, unknown>> {
  readonly name: string;
  readonly description: string;
  readonly inputSchema?: object;
  readonly annotations?: { readonly readOnlyHint?: boolean; readonly untrustedContentHint?: boolean };
  /** Server state decides this, never the browser's own idea of the phase. */
  readonly enabled: boolean;
  /** Larger budget only where §11.4 grants one. */
  readonly resultLimit?: number;
  readonly execute: (args: Args) => Promise<unknown>;
}

/**
 * Register one tool through the pinned hook, for as long as `enabled` holds.
 *
 * `enabled` comes from the caller and must be derived from **server** state:
 * FastAPI is authoritative, and a tool that decided its own availability from a
 * stale browser snapshot would offer an action the server then refuses (§11.5).
 */
export function useHarnessTool<Args = Record<string, unknown>>(
  tool: HarnessToolDefinition<Args>,
): RegistrationState {
  const limit = tool.resultLimit ?? MAX_TOOL_RESULT_CHARS;

  // Callers pass inline closures — a panel writing `execute: async () => …`
  // creates a new function every render. Holding the latest in a ref keeps this
  // handler's identity stable, so the registration below is not torn down and
  // rebuilt on every render (which is an infinite loop, not merely churn).
  const latest = useRef(tool.execute);
  latest.current = tool.execute;

  const execute = useCallback(
    async (args: Args): Promise<NormalizedToolResult> => {
      try {
        return normalizeResult(await latest.current(args), limit);
      } catch (error: unknown) {
        // Normalized rather than rethrown: a rejected promise reaches an agent
        // as a transport failure, which tells it nothing about what to do next.
        return normalizeError(error, limit);
      }
    },
    [limit],
  );

  const state = useWebMCP<Args, NormalizedToolResult>({
    name: tool.name,
    description: tool.description,
    ...(tool.inputSchema === undefined ? {} : { inputSchema: tool.inputSchema }),
    ...(tool.annotations === undefined ? {} : { annotations: tool.annotations }),
    enabled: tool.enabled,
    execute,
    // The hook would otherwise wrap our already-normalized result again.
    formatOutput: (result) => result,
  });

  return phaseOf(state);
}

function phaseOf(state: {
  supported: boolean;
  registered: boolean;
  error: Error | null;
}): RegistrationState {
  if (!state.supported) {
    return UNSUPPORTED;
  }
  if (state.error !== null) {
    return { phase: "failed", detail: state.error.message };
  }
  return state.registered
    ? { phase: "registered", detail: "Registered." }
    : { phase: "registering", detail: "Registering…" };
}

export interface NativeToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly inputSchema?: object;
  readonly annotations?: { readonly readOnlyHint?: boolean; readonly untrustedContentHint?: boolean };
  readonly enabled: boolean;
  readonly resultLimit?: number;
  /**
   * Receives the invocation's own `AbortSignal`.
   *
   * This is the whole reason the native path exists: the pinned hook's
   * `execute` takes only its arguments, so a handler registered through it
   * cannot learn that its caller walked away.
   */
  readonly execute: (
    args: Record<string, unknown>,
    context: { readonly signal: AbortSignal | undefined },
  ) => Promise<unknown>;
}

/**
 * Register one tool directly, forwarding each invocation's signal.
 *
 * Used where cancellation matters. A `proceed_to_checkout` whose caller
 * disappeared must cancel its pending confirmation rather than leave a human
 * staring at a dialog nobody is waiting on (§14.9).
 */
export function useNativeTool(tool: NativeToolDefinition): RegistrationState {
  const [state, setState] = useState<RegistrationState>(UNSUPPORTED);
  const limit = tool.resultLimit ?? MAX_TOOL_RESULT_CHARS;
  const { name, description, enabled } = tool;

  // The whole definition is held in a ref. Call sites write inline closures and
  // object literals — `execute: async () => …`, `inputSchema: { … }` — so every
  // render produces fresh identities, and an effect keyed on them would tear
  // the registration down and rebuild it forever rather than merely churn.
  const latest = useRef(tool);
  latest.current = tool;

  // What the effect keys on instead: the values that actually change what is
  // registered, compared by content rather than by identity.
  const shape = JSON.stringify({
    inputSchema: tool.inputSchema ?? null,
    annotations: tool.annotations ?? null,
  });

  useEffect(() => {
    if (!enabled) {
      setState({ phase: "registering", detail: "Not available in this state." });
      return;
    }
    const modelContext = document.modelContext;
    if (modelContext === undefined) {
      setState(UNSUPPORTED);
      return;
    }

    const controller = new AbortController();
    let live = true;
    setState({ phase: "registering", detail: `Registering ${name}…` });

    void modelContext
      .registerTool(
        {
          name,
          description,
          ...(latest.current.inputSchema === undefined
            ? {}
            : { inputSchema: latest.current.inputSchema }),
          ...(latest.current.annotations === undefined
            ? {}
            : { annotations: latest.current.annotations }),
          // context is OPTIONAL at runtime: ADR-0002 recorded that the pinned
          // Chrome build's executeTool invokes handlers with no context at all
          // (no per-invocation signal), and the Tier 1 gate run proved an
          // unguarded `context.signal` crashes every native invocation there.
          // The signal is a responsiveness improvement when present, never a
          // precondition.
          execute: async (args: unknown, context?: { signal?: AbortSignal }) => {
            try {
              return normalizeResult(
                await latest.current.execute((args ?? {}) as Record<string, unknown>, {
                  signal: context?.signal,
                }),
                limit,
              );
            } catch (error: unknown) {
              return normalizeError(error, limit);
            }
          },
        } as unknown as WebMCP.ModelContextTool,
        { signal: controller.signal },
      )
      .then(
        () => {
          // Reject a completion that lost its race with unmount, or
          // StrictMode's first pass overwrites the second pass's state.
          if (live) {
            setState({ phase: "registered", detail: "Registered." });
          }
        },
        (error: unknown) => {
          if (live) {
            setState({
              phase: "failed",
              detail: error instanceof Error ? error.message : String(error),
            });
          }
        },
      );

    return () => {
      live = false;
      // Aborting *is* the unregistration, so the cleanup cannot be forgotten
      // separately from the registration it undoes.
      controller.abort();
    };
  }, [name, description, enabled, limit, shape]);

  return state;
}

export interface RawToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly inputSchema?: object;
  readonly enabled: boolean;
  /**
   * Already shaped as WebMCP's own wire result (`WebMCP.ToolExecuteCallback`'s
   * return, e.g. `{ content: [...] }`) rather than a business value bound for
   * `normalizeResult`. Typed directly against the pinned package so no escape
   * hatch is needed at the call site — see the function doc for why that
   * distinction exists.
   */
  readonly execute: WebMCP.ToolExecuteCallback;
}

/**
 * Register a tool exactly as the caller shaped it: no result normalization,
 * no invocation-signal forwarding, and no re-derivation of the wire shape.
 *
 * This is deliberately narrower than `useNativeTool` and `useHarnessTool`, and
 * exists for one caller: `usePoisonedToolSurface` (§13.3's injected
 * mid-session tool-surface fault). That fixture already returns
 * `{ content: [...] }` — the wire shape itself, because the point of the demo
 * is what a look-alike *tool definition* looks like to `getTools()`, not a
 * business value for this adapter to wrap. Routing it through
 * `useNativeTool` would re-stringify an already-shaped result via
 * `normalizeResult`, changing what the injected tool actually returns and
 * quietly fixing the fixture's observable misbehaviour along the way. Prefer
 * `useNativeTool` or `useHarnessTool` for anything that is not this.
 *
 * The lifecycle is the same as `useNativeTool`'s: registration keyed on
 * content rather than closure identity, an `AbortController` whose abort
 * *is* the unregistration, and a `live` guard so a registration that resolves
 * after StrictMode's first pass unmounted cannot overwrite the second pass's
 * state.
 */
export function useRawNativeTool(tool: RawToolDefinition): RegistrationState {
  const [state, setState] = useState<RegistrationState>(UNSUPPORTED);
  const { name, description, enabled } = tool;

  const latest = useRef(tool);
  latest.current = tool;

  const shape = JSON.stringify(tool.inputSchema ?? null);

  useEffect(() => {
    if (!enabled) {
      setState({ phase: "registering", detail: "Not available in this state." });
      return;
    }
    const modelContext = document.modelContext;
    if (modelContext === undefined) {
      setState(UNSUPPORTED);
      return;
    }

    const controller = new AbortController();
    let live = true;
    setState({ phase: "registering", detail: `Registering ${name}…` });

    void modelContext
      .registerTool(
        {
          name,
          description,
          ...(latest.current.inputSchema === undefined
            ? {}
            : { inputSchema: latest.current.inputSchema }),
          execute: (args, context) => latest.current.execute(args, context),
        },
        { signal: controller.signal },
      )
      .then(
        () => {
          if (live) {
            setState({ phase: "registered", detail: "Registered." });
          }
        },
        (error: unknown) => {
          if (live) {
            setState({
              phase: "failed",
              detail: error instanceof Error ? error.message : String(error),
            });
          }
        },
      );

    return () => {
      live = false;
      controller.abort();
    };
  }, [name, description, enabled, shape]);

  return state;
}

/**
 * §25.2's declarative registration — the third mechanism, and the odd one out.
 *
 * There is no `registerTool` call here because there is nothing to call. The
 * browser reads `toolname` and `tooldescription` off a visible `<form>`, its
 * `toolparamdescription` controls become the schema, and the tool appears
 * because the markup exists. That is the whole appeal: the agent's affordance
 * and the human's affordance are the same DOM node, so they cannot drift apart
 * the way a hand-written schema drifts from the form it claims to describe.
 *
 * It still belongs in this module. The attribute names, `agentInvoked`,
 * `respondWith`, and the `toolactivated`/`toolcancel` events are all direct
 * WebMCP surface, and the constitution keeps that in the adapter. A component
 * gets prop objects and a submit handler; it never learns an attribute name.
 *
 * ## What `respondWith` is for
 *
 * A human submitting the form gets a page that updates. An agent submitting it
 * needs a *result* — and the submit handler's promise is the only thing that
 * knows when the server actually answered. Without `respondWith`, the agent's
 * call resolves the instant the handler returns, which is before the contract
 * exists; it would read a pending request as a finished one.
 *
 * ## Why the activation state is rendered
 *
 * §25.2 asks for `toolactivated` and `toolcancel` to be handled "so agent focus
 * and cancellation remain visible". A form quietly filled in and submitted by
 * something the person cannot see is precisely the failure mode this product
 * exists to make visible, so the state is surfaced rather than merely tracked.
 */

export type AgentActivity = "idle" | "activated" | "cancelled";

export interface DeclarativeToolDefinition {
  readonly name: string;
  readonly description: string;
}

/** What a control needs to become a parameter of the declarative tool. */
export function toolParameterProps(description: string): Record<string, string> {
  return { toolparamdescription: description };
}

/** The submit control an agent may operate (§25.2's `toolautosubmit`). */
export function toolAutoSubmitProps(): Record<string, string> {
  return { toolautosubmit: "" };
}

export interface DeclarativeFormBinding {
  /** Spread onto the `<form>`: this is the registration. */
  readonly formProps: Record<string, string>;
  readonly ref: MutableRefObject<HTMLFormElement | null>;
  readonly onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  /** Whether an agent currently holds the form, for the human looking at it. */
  readonly activity: AgentActivity;
}

/**
 * Bind one visible form as a declarative tool.
 *
 * `submit` is given the form's own values and returns whatever the agent should
 * receive. It is the *same* function a human submission runs — §25.2 requires
 * the declarative path to "post the same payload to FastAPI used by a human
 * submission", and the way to guarantee that is to have one handler rather than
 * two that are supposed to agree.
 */
export function useDeclarativeTool(
  tool: DeclarativeToolDefinition,
  submit: (values: FormData) => Promise<unknown>,
): DeclarativeFormBinding {
  const ref = useRef<HTMLFormElement | null>(null);
  const [activity, setActivity] = useState<AgentActivity>("idle");

  // Held in a ref for the same reason the other two paths do it: call sites
  // write inline closures, and an effect keyed on this identity would rebind
  // the listeners on every render.
  const latest = useRef(submit);
  latest.current = submit;

  useEffect(() => {
    const form = ref.current;
    if (form === null) {
      return;
    }
    const activated = (): void => {
      setActivity("activated");
    };
    const cancelled = (): void => {
      setActivity("cancelled");
    };
    form.addEventListener("toolactivated", activated);
    form.addEventListener("toolcancel", cancelled);
    return () => {
      form.removeEventListener("toolactivated", activated);
      form.removeEventListener("toolcancel", cancelled);
    };
  }, []);

  const onSubmit = useCallback((event: FormEvent<HTMLFormElement>): void => {
    // Always. A declarative form that navigated would tear down the page the
    // agent is mid-conversation with, and the human's own submission would lose
    // every other panel's state.
    event.preventDefault();

    const values = new FormData(event.currentTarget);
    const answered = latest.current(values).then(
      (result) => normalizeResult(result),
      (error: unknown) => normalizeError(error),
    );

    // `agentInvoked` is the only thing that distinguishes the two callers, and
    // it decides one thing: whether anybody is waiting for a value. A human
    // gets the re-rendered page; an agent gets this promise.
    const submitEvent = event.nativeEvent as SubmitEvent & {
      agentInvoked?: boolean;
      respondWith?: (result: Promise<unknown>) => void;
    };
    if (submitEvent.agentInvoked === true && typeof submitEvent.respondWith === "function") {
      submitEvent.respondWith(answered);
    }
    setActivity("idle");
  }, []);

  return {
    formProps: { toolname: tool.name, tooldescription: tool.description },
    ref,
    onSubmit,
    activity,
  };
}

/**
 * One tool as the browser reports it, narrowed (FR-166, FR-167).
 *
 * No `identity_hash` and no `namespace`: the server computes both. Adding
 * either here would not merely be redundant — it would move a decision the
 * server must own onto the least trustworthy side of the boundary.
 */
export interface CapturedTool {
  readonly name: string;
  readonly description: string;
  readonly read_only_hint: boolean | null;
  readonly untrusted_content_hint: boolean | null;
  readonly input_schema: Record<string, unknown>;
}

/**
 * Narrow one descriptor from `getTools()`.
 *
 * Everything arrives as `unknown`: these objects come from the browser's tool
 * registry, which any script on the origin can write to. A descriptor missing a
 * usable name is dropped rather than defaulted — a tool the server cannot name
 * is one it cannot compare against a baseline, and inventing a name would
 * invent a delta.
 */
export function describeTool(value: unknown): CapturedTool | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const name = record["name"];
  if (typeof name !== "string" || name.length === 0) {
    return null;
  }
  return {
    name,
    description: typeof record["description"] === "string" ? record["description"] : "",
    // Absent stays absent. A tool that stopped *declaring* itself read-only
    // changed its hints, and coercing the absence to `false` would hide that.
    read_only_hint: hintOf(record, "readOnlyHint"),
    untrusted_content_hint: hintOf(record, "untrustedContentHint"),
    input_schema: isPlainRecord(record["inputSchema"]) ? record["inputSchema"] : {},
  };
}

/**
 * One behavioural hint from a `getTools()` descriptor.
 *
 * `webmcp-types` nests both hints inside `RegisteredTool.annotations`, so that
 * is where they are read from. Until this was fixed the top level was read
 * instead and every captured hint was therefore `null` — which made
 * `hint_change` a delta kind no run could ever produce, silently, while
 * `one_mug_stable_surface` listed it among the kinds that must fail a run.
 *
 * The top level is still accepted as a fallback. Both readings come from the
 * same registry and carry exactly the same trust — none — so preferring the
 * declared shape while tolerating a flattened one costs nothing and keeps the
 * capture working against a browser that reports the older layout.
 */
function hintOf(record: Record<string, unknown>, name: string): boolean | null {
  const annotations = record["annotations"];
  if (isPlainRecord(annotations) && typeof annotations[name] === "boolean") {
    return annotations[name];
  }
  return typeof record[name] === "boolean" ? record[name] : null;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Read the whole surface, or `null` when this browser has no WebMCP.
 *
 * **The one `getTools()` call in the product.** Both things that need the
 * surface go through here — the registration view a person reads and the
 * capture the server judges — so the two cannot end up looking at different
 * reads and disagreeing about what is registered. A page that showed "all
 * registered" while the evidence recorded something else would be worse than
 * one that showed nothing.
 */
export async function readSurface(): Promise<readonly CapturedTool[] | null> {
  const modelContext = document.modelContext;
  if (modelContext === undefined) {
    return null;
  }
  const tools = await modelContext.getTools();
  return tools
    .map((tool) => describeTool(tool))
    .filter((tool): tool is CapturedTool => tool !== null);
}

/**
 * Subscribe to `toolchange`, or `null` when this browser has no WebMCP.
 *
 * The caller gets an unsubscribe rather than an event target, so nothing
 * outside this module needs to hold `document.modelContext` to listen. Both
 * things that watch the surface — the registration view and the capture the
 * server judges — subscribe here, which is what keeps them from watching
 * different objects and disagreeing about when the surface changed.
 *
 * `null` rather than a no-op unsubscribe: "there is no WebMCP" is a case the
 * caller must handle, not one to paper over. The surface witness in particular
 * has to *stop* there, because a run with no baseline is an explicit non-pass
 * at verification (§16.1) rather than a run that quietly captured nothing.
 */
export function subscribeToToolChange(onChange: () => void): (() => void) | null {
  const modelContext = document.modelContext;
  if (modelContext === undefined) {
    return null;
  }
  modelContext.addEventListener("toolchange", onChange);
  return () => {
    modelContext.removeEventListener("toolchange", onChange);
  };
}

/**
 * What one group of tools claims, so the reconciliation has something to check.
 *
 * `declared` is every tool the group can register in any phase; `claimed` is
 * the subset this app currently believes is registered. The distinction keeps
 * a tool that is *deliberately* unavailable — §11.5 changes the visible set
 * with the workspace phase — from reading as one that failed to register.
 */
export interface ToolExpectation {
  readonly declared: readonly string[];
  readonly claimed: readonly string[];
}

/**
 * Derive an expectation from a toolset's registration states.
 *
 * Taken from the states the registrations actually produced rather than from a
 * hand-written list, so a tool added to a toolset cannot be forgotten here and
 * quietly become "unexpected" — which would report the product's own tool as a
 * stranger on the origin.
 */
export function expectationOf(
  states: Readonly<Record<string, RegistrationState>>,
): ToolExpectation {
  const declared = Object.keys(states);
  return {
    declared,
    claimed: declared.filter((name) => states[name]?.phase === "registered"),
  };
}

export interface ToolGroupReconciliation {
  /** Every tool this group can register in some phase. */
  readonly declared: readonly string[];
  /** The subset this app believes it registered. */
  readonly claimed: readonly string[];
  /**
   * Declared *and* reported by the browser — FR-003's "whether ... tools are
   * registered", answered by the browser rather than by this app.
   *
   * Measured against `declared` rather than `claimed` so it can also account
   * for the declarative tool, which the app never claims: nothing here called
   * `registerTool` for it, so the browser's answer is the only evidence there
   * is that the markup was read.
   */
  readonly present: readonly string[];
  /**
   * Claimed but absent from `getTools()` — the disagreement worth showing.
   *
   * A registration can fail after the effect that started it returned. Mount
   * state alone would call that a success, which is the inference FR-003
   * forbids.
   */
  readonly missing: readonly string[];
}

export interface ToolReconciliation {
  readonly supported: boolean;
  /** How many tools the browser reports, in total (FR-003). */
  readonly count: number;
  readonly harness: ToolGroupReconciliation;
  readonly target: ToolGroupReconciliation;
  /**
   * Reported by the browser and declared by neither group.
   *
   * Surfaced, never swallowed. This view has no authority to call an extra tool
   * acceptable — `stable_tool_surface` decides that from recorded evidence, and
   * a reconciliation that quietly accepted a name would be a second, softer
   * opinion about the exact thing the policy exists to judge.
   */
  readonly unexpected: readonly string[];
}

const NOTHING: ToolGroupReconciliation = {
  declared: [],
  claimed: [],
  present: [],
  missing: [],
};

function reconcile(
  expectation: ToolExpectation,
  reported: ReadonlySet<string>,
): ToolGroupReconciliation {
  return {
    declared: expectation.declared,
    claimed: expectation.claimed,
    present: expectation.declared.filter((name) => reported.has(name)),
    missing: expectation.claimed.filter((name) => !reported.has(name)),
  };
}

/**
 * Reconcile registration status against the browser (FR-003).
 *
 * FR-003: "The UI shall reconcile registration status against
 * `document.modelContext.getTools()` and the `toolchange` event, then show
 * whether harness and selected-target tools are registered and the number
 * currently available. It shall not infer success solely from React component
 * mount state."
 *
 * The last sentence is the requirement. A mounted effect proves a registration
 * was *attempted*; only the browser knows whether one exists. The two genuinely
 * disagree — a registration can fail after the effect that started it returned,
 * and another script on the origin can register tools this app never mounted —
 * so the comparison is between what this app claims and what the browser
 * reports, re-read on every `toolchange`.
 *
 * This is diagnosis, not judgement. Nothing here decides whether a surface is
 * acceptable: that is `stable_tool_surface`, evaluated by the server from
 * recorded evidence, and the panel says so.
 */
export function useToolReconciliation(
  harness: ToolExpectation,
  target: ToolExpectation,
): ToolReconciliation {
  const [reported, setReported] = useState<readonly string[] | null>(null);

  useEffect(() => {
    let live = true;
    const refresh = (): void => {
      void readSurface().then(
        (tools) => {
          // Ignore a read that resolved after unmount: it would write state
          // belonging to a page that has gone.
          if (live) {
            setReported(tools === null ? null : tools.map((tool) => tool.name));
          }
        },
        () => {
          // A failed read is not an empty surface. Reporting one would show
          // every tool as missing and invite somebody to go looking for a
          // registration bug that is not there.
          if (live) {
            setReported(null);
          }
        },
      );
    };

    const unsubscribe = subscribeToToolChange(refresh);
    if (unsubscribe === null) {
      setReported(null);
      return;
    }
    refresh();

    return () => {
      live = false;
      unsubscribe();
    };
  }, []);

  if (reported === null) {
    return { supported: false, count: 0, harness: NOTHING, target: NOTHING, unexpected: [] };
  }

  const names = new Set(reported);
  const declared = new Set([...harness.declared, ...target.declared]);
  return {
    supported: true,
    count: reported.length,
    harness: reconcile(harness, names),
    target: reconcile(target, names),
    // Compared against everything *declared*, not everything claimed: a tool
    // still mid-registration is ours, and flagging it as a stranger would cry
    // wolf on every page load.
    unexpected: reported.filter((name) => !declared.has(name)),
  };
}
